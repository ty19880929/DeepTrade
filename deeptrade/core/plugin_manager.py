"""Plugin install / validate / uninstall / upgrade.

DESIGN §8.3 + S1 (migrations are the sole DDL source) + S2 (install never
touches the network) + M3 (llm_tools=true is rejected).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import shutil
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from deeptrade.core import paths
from deeptrade.core.db import Database
from deeptrade.core.dep_installer import (
    DepInstallError,
    parse_specs,
    plan_install,
    run_install,
)
from deeptrade.plugins_api.base import Plugin
from deeptrade.plugins_api.metadata import (
    RESERVED_TABLE_NAMES,
    MigrationSpec,
    PluginMetadata,
)

logger = logging.getLogger(__name__)

CURRENT_API_VERSION = "1"
# v0.6 — both legacy v1 and ctx-aware v2 dispatch are first-class. New
# plugin authors should use "2"; "1" is supported without deprecation.
SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"1", "2"})

# Reserved framework-level command names. A plugin_id colliding with any of
# these would shadow framework dispatch and is rejected at install time.
RESERVED_PLUGIN_IDS: frozenset[str] = frozenset({"init", "config", "plugin", "data"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PluginError(Exception):
    """Generic plugin manager error."""


class PluginInstallError(PluginError):
    """Install pipeline failure."""


class PluginNotFoundError(PluginError):
    """No such installed plugin."""


# ---------------------------------------------------------------------------
# Records (lightweight DTOs over the DB rows)
# ---------------------------------------------------------------------------


@dataclass
class InstalledPlugin:
    plugin_id: str
    name: str
    version: str
    type: str
    api_version: str
    entrypoint: str
    install_path: str
    enabled: bool
    metadata: PluginMetadata


@dataclass
class UpgradeNoop:
    """Returned by :meth:`PluginManager.upgrade` when the candidate version
    equals the installed version (no-op)."""

    plugin_id: str
    version: str


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_metadata_yaml(yaml_path: Path) -> PluginMetadata:
    if not yaml_path.is_file():
        raise PluginInstallError(f"metadata file not found: {yaml_path}")
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PluginInstallError(f"metadata must be a YAML mapping: {yaml_path}")
    try:
        return PluginMetadata.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        raise PluginInstallError(f"invalid metadata in {yaml_path}: {e}") from e


def _verify_migration_checksum(plugin_root: Path, mig: MigrationSpec) -> str:
    """Read mig.file and compare to mig.checksum (sha256:<hex>). Returns the
    SQL text on success."""
    sql_path = plugin_root / mig.file
    if not sql_path.is_file():
        raise PluginInstallError(f"migration file missing: {mig.file}")
    sql_text = sql_path.read_text(encoding="utf-8")
    actual = "sha256:" + hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
    if actual != mig.checksum:
        raise PluginInstallError(
            f"checksum mismatch for {mig.file}: expected {mig.checksum}, got {actual}"
        )
    return sql_text


def _load_entrypoint(
    install_path: Path,
    entrypoint: str,
    metadata: PluginMetadata | None = None,
) -> Plugin:
    """Load ``module.path:Class`` from the installed plugin directory and
    instantiate it.

    Uses ``sys.path`` insertion + ``importlib.import_module`` so that intra-plugin
    relative imports (``from .calendar import TradeCalendar``) work — these would
    fail with ``spec_from_file_location`` because the parent package would not
    be initialized.

    When ``metadata`` is supplied, it is set on the resulting instance so
    plugins can read ``self.metadata`` at runtime.

    v0.5 guards:

    * T07 — capture ``sys.modules["deeptrade"]``'s identity before the
      import and refuse the install if the plugin's import somehow
      replaced the framework module object. The metadata-time T04 check
      already blocks the obvious case (entrypoint top package =
      ``deeptrade``); this is the runtime backstop for indirect shadowing
      (e.g. a plugin file that performs its own ``sys.path`` manipulation
      or imports a sibling ``deeptrade.py``).
    * T08 — verify the loaded instance satisfies the
      :class:`~deeptrade.plugins_api.base.Plugin` runtime-checkable
      Protocol before returning. Without this, a class missing
      ``dispatch`` only fails much later at first invocation.
    """
    module_path, _, class_name = entrypoint.partition(":")
    if not module_path or not class_name:
        raise PluginInstallError(f"bad entrypoint: {entrypoint}")

    install_str = str(install_path)
    top_pkg_name = module_path.split(".", 1)[0]

    # Verify the module file actually lives under install_path BEFORE touching sys.path
    expected_pkg_dir = install_path / top_pkg_name
    if not expected_pkg_dir.is_dir():
        # Try single-file leaf (rare, but supported)
        if not (install_path / (module_path.replace(".", "/") + ".py")).is_file():
            raise PluginInstallError(f"cannot locate module {module_path!r} under {install_path}")

    # T07: snapshot the framework module so we can detect (and undo)
    # accidental shadowing. ``__file__`` is on every regular Python module;
    # using it as the comparison key is cheap and immune to a plugin that
    # re-registers a different object under the same name.
    framework_mod = sys.modules.get("deeptrade")
    framework_file = getattr(framework_mod, "__file__", None)

    # Evict any cached copy of this plugin's top package so install_path is used
    for cached in [m for m in sys.modules if m == top_pkg_name or m.startswith(top_pkg_name + ".")]:
        sys.modules.pop(cached, None)

    sys.path.insert(0, install_str)
    try:
        module = importlib.import_module(module_path)
    except Exception as e:
        raise PluginInstallError(f"cannot import {module_path}: {e}") from e
    finally:
        # Don't leave install_path on sys.path; the module objects already imported
        # are cached in sys.modules and remain usable by reference.
        if install_str in sys.path:
            sys.path.remove(install_str)

    # T07 check: if the framework module was replaced during import, restore
    # it and reject the plugin. We cannot continue safely — every subsequent
    # `from deeptrade.x import y` would resolve against the plugin's tree.
    post_framework_mod = sys.modules.get("deeptrade")
    post_framework_file = getattr(post_framework_mod, "__file__", None)
    if framework_mod is not None and (
        post_framework_mod is not framework_mod or post_framework_file != framework_file
    ):
        sys.modules["deeptrade"] = framework_mod
        raise PluginInstallError(
            f"plugin entrypoint {entrypoint!r} replaced the framework "
            f"'deeptrade' module on import (was {framework_file!r}, "
            f"became {post_framework_file!r}); refusing to continue"
        )

    if not hasattr(module, class_name):
        raise PluginInstallError(f"{module_path} has no class {class_name}")
    plugin_cls = getattr(module, class_name)
    instance = plugin_cls()
    if metadata is not None:
        instance.metadata = metadata

    # T08: verify the plugin contract. Plugin Protocol is @runtime_checkable
    # so this is a structural check (validate_static + dispatch + metadata
    # attribute present), not nominal inheritance.
    if not isinstance(instance, Plugin):
        raise PluginInstallError(
            f"entrypoint {entrypoint!r} class {class_name!r} does not implement "
            f"the Plugin protocol (must define metadata, validate_static, dispatch)"
        )
    return instance


def _build_validate_ctx(db: Database, meta: PluginMetadata) -> Any:
    """Build the framework's minimal ``PluginContext`` for ``validate_static``.

    Every plugin shares the same narrow context shape: db + config + plugin_id.
    Plugins that need richer services (TushareClient, LLMManager / LLMClient, ...)
    construct them inside their own ``dispatch`` from these primitives.
    """
    from deeptrade.core.config import ConfigService
    from deeptrade.plugins_api.base import PluginContext

    return PluginContext(db=db, config=ConfigService(db), plugin_id=meta.plugin_id)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class PluginManager:
    def __init__(self, db: Database, install_root: Path | None = None) -> None:
        self._db = db
        self._install_root = install_root or paths.plugins_dir()
        self._install_root.mkdir(parents=True, exist_ok=True)

    # --- install -----------------------------------------------------

    def install(
        self,
        source_path: Path,
        *,
        install_deps: bool = True,
        reinstall_deps: bool = False,
    ) -> InstalledPlugin:
        """Install a plugin from a local directory. Network never touched
        by plugin code; framework may run pip/uv if ``install_deps=True``."""
        source_path = source_path.resolve()
        if not source_path.is_dir():
            raise PluginInstallError(f"source path is not a directory: {source_path}")

        meta = _load_metadata_yaml(source_path / "deeptrade_plugin.yaml")

        if meta.plugin_id in RESERVED_PLUGIN_IDS:
            raise PluginInstallError(
                f"plugin_id {meta.plugin_id!r} is reserved by the framework "
                f"(reserved: {sorted(RESERVED_PLUGIN_IDS)})"
            )

        if meta.api_version not in SUPPORTED_API_VERSIONS:
            raise PluginInstallError(
                f"plugin api_version {meta.api_version!r} not supported by framework "
                f"(supported: {sorted(SUPPORTED_API_VERSIONS)})"
            )

        # M3 hard-constraint enforcement (Pydantic Literal[False] also catches it)
        if meta.permissions.llm_tools is not False:
            raise PluginInstallError("permissions.llm_tools=true is forbidden")

        # Uniqueness
        existing = self._fetch_one_plugin(meta.plugin_id)
        if existing is not None:
            raise PluginInstallError(
                f"plugin_id {meta.plugin_id!r} already installed at {existing.install_path}; "
                f"use `plugin upgrade` for version change"
            )

        # Verify migration checksums BEFORE copy / DB writes
        mig_sql: list[tuple[MigrationSpec, str]] = []
        for mig in meta.migrations:
            sql_text = _verify_migration_checksum(source_path, mig)
            mig_sql.append((mig, sql_text))

        # Copy to ~/.deeptrade/plugins/installed/<plugin_id>/<version>/
        target = self._install_root / meta.plugin_id / meta.version
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)

        # Resolve & install Python deps BEFORE migrations + entrypoint load.
        # validate_static imports the plugin module — deps must be importable
        # by then. On failure, just remove the copied dir; already-installed
        # deps stay in the env (see design §4.5).
        if install_deps:
            try:
                self._handle_dependencies(meta, reinstall=reinstall_deps)
            except DepInstallError as e:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                raise PluginInstallError(str(e)) from e

        # Apply migrations + write registries inside ONE transaction.
        try:
            with self._db.transaction():
                applied = self._apply_migrations(meta.plugin_id, meta, mig_sql)
                self._record_plugin(meta, target)
                self._record_tables(meta)
                self._record_migrations(meta.plugin_id, applied)

                # Verify each declared table actually exists post-migration
                missing = self._missing_declared_tables(meta)
                if missing:
                    raise PluginInstallError(
                        f"declared tables not created by migrations: {sorted(missing)}"
                    )
        except Exception:
            # rollback: also remove the copied directory
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

        # B2.2 — Static self-check (no network) MUST gate install acceptance.
        # Failure → roll back DB rows + remove install copy + raise.
        try:
            instance = _load_entrypoint(target, meta.entrypoint, meta)
            # T08: ``_load_entrypoint`` already verified the Plugin protocol,
            # so validate_static is guaranteed callable; the explicit nil
            # check is defense in depth against runtime monkey-patching.
            validate = getattr(instance, "validate_static", None)
            if validate is not None:
                validate(_build_validate_ctx(self._db, meta))
        except Exception as e:
            # Roll back: drop the just-installed plugin tables + delete registry rows + remove copy
            self._rollback_install(meta, target)
            raise PluginInstallError(
                f"validate_static / entrypoint load failed for {meta.plugin_id}: {e}"
            ) from e

        return self._compose_record(meta, target, enabled=True)

    def _rollback_install(self, meta: PluginMetadata, target: Path) -> None:
        """Undo a partially-completed install. Idempotent."""
        with self._db.transaction():
            # Drop owned tables (best-effort)
            for t in meta.tables:
                if t.purge_on_uninstall:
                    try:
                        self._db.execute(f"DROP TABLE IF EXISTS {t.name}")  # noqa: S608
                    except Exception:  # noqa: BLE001
                        pass
            self._db.execute("DELETE FROM plugin_tables WHERE plugin_id = ?", (meta.plugin_id,))
            self._db.execute(
                "DELETE FROM plugin_schema_migrations WHERE plugin_id = ?", (meta.plugin_id,)
            )
            self._db.execute("DELETE FROM plugins WHERE plugin_id = ?", (meta.plugin_id,))
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    # --- list / info / disable / enable / uninstall / upgrade --------

    def list_all(self) -> list[InstalledPlugin]:
        """List all installed plugins (renamed from `list` to avoid shadowing the builtin)."""
        rows = self._db.fetchall(
            "SELECT plugin_id, name, version, type, api_version, entrypoint, "
            "install_path, enabled, metadata_yaml FROM plugins ORDER BY plugin_id"
        )
        return [self._row_to_record(r) for r in rows]

    def info(self, plugin_id: str) -> InstalledPlugin:
        rec = self._fetch_one_plugin(plugin_id)
        if rec is None:
            raise PluginNotFoundError(f"plugin not installed: {plugin_id}")
        return rec

    def disable(self, plugin_id: str) -> None:
        if self._fetch_one_plugin(plugin_id) is None:
            raise PluginNotFoundError(f"plugin not installed: {plugin_id}")
        self._db.execute("UPDATE plugins SET enabled = FALSE WHERE plugin_id = ?", (plugin_id,))

    def enable(self, plugin_id: str) -> None:
        rec = self._fetch_one_plugin(plugin_id)
        if rec is None:
            raise PluginNotFoundError(f"plugin not installed: {plugin_id}")
        # F-L1 — guard against enabling a plugin whose install_path was
        # removed (e.g. by an earlier uninstall without --purge that wiped
        # the on-disk copy). Re-enabling such a record would later crash
        # the runner with a confusing ImportError.
        if not Path(rec.install_path).exists():
            raise PluginInstallError(
                f"plugin {plugin_id!r} install_path missing ({rec.install_path}); "
                f"reinstall before enabling"
            )
        self._db.execute("UPDATE plugins SET enabled = TRUE WHERE plugin_id = ?", (plugin_id,))

    def uninstall(self, plugin_id: str, *, purge: bool = False) -> dict[str, Any]:
        rec = self._fetch_one_plugin(plugin_id)
        if rec is None:
            raise PluginNotFoundError(f"plugin not installed: {plugin_id}")

        dropped: list[str] = []
        if purge:
            dropped = self._purge_plugin_tables(plugin_id)
        else:
            # default: just disable + remove the install copy
            self._db.execute("UPDATE plugins SET enabled = FALSE WHERE plugin_id = ?", (plugin_id,))

        # remove the on-disk install copy (idempotent)
        install_path = Path(rec.install_path)
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)

        return {"purged_tables": dropped, "purge": purge}

    def _purge_plugin_tables(self, plugin_id: str) -> list[str]:
        """Drop the plugin's tables + delete its registry rows (T06).

        Defense in depth on top of T01 (metadata-time guard) and T05
        (migration-time guard):

        * If ``plugin_schema_migrations.affected_tables`` is populated for
          every migration row, the union of those lists is the authoritative
          set of tables this plugin created — intersect with ``plugin_tables``
          honoring ``purge_on_uninstall=False`` declarations.
        * If any row has ``affected_tables IS NULL`` (legacy v0.4 install),
          fall back to ``plugin_tables`` only — we can't trust an
          incomplete migration record.
        * In either case, refuse to DROP anything in
          :data:`RESERVED_TABLE_NAMES`. This catches corrupted DB state
          (a row manually INSERTed into ``plugin_tables`` claiming a
          framework table) that bypassed earlier guards.
        """
        mig_rows = self._db.fetchall(
            "SELECT affected_tables FROM plugin_schema_migrations WHERE plugin_id = ?",
            (plugin_id,),
        )
        has_null = any(r[0] is None for r in mig_rows) or not mig_rows
        recorded: set[str] = set()
        for (affected_json,) in mig_rows:
            if affected_json is None:
                continue
            try:
                names = json.loads(affected_json)
            except json.JSONDecodeError:
                logger.warning(
                    "plugin %s: malformed affected_tables JSON %r; "
                    "falling back to plugin_tables for purge",
                    plugin_id,
                    affected_json,
                )
                has_null = True
                continue
            if not isinstance(names, list):
                has_null = True
                continue
            recorded.update(str(n) for n in names)

        purge_flags: dict[str, bool] = {
            r[0]: bool(r[1])
            for r in self._db.fetchall(
                "SELECT table_name, purge_on_uninstall FROM plugin_tables WHERE plugin_id = ?",
                (plugin_id,),
            )
        }

        if has_null:
            # Legacy fallback: drop every table_name in plugin_tables that
            # asked to be purged on uninstall.
            candidates = {t for t, p in purge_flags.items() if p}
        else:
            # Trust the framework-captured record. Intersect with
            # plugin_tables so purge_on_uninstall=False is still honored.
            opt_in = {t for t, p in purge_flags.items() if p}
            candidates = recorded & opt_in

        dropped: list[str] = []
        with self._db.transaction():
            for tname in sorted(candidates):
                if tname in RESERVED_TABLE_NAMES:
                    logger.error(
                        "refusing to DROP framework-reserved table %r during "
                        "purge of plugin %r (corrupted plugin_tables row?)",
                        tname,
                        plugin_id,
                    )
                    continue
                self._db.execute(f"DROP TABLE IF EXISTS {tname}")  # noqa: S608 — name validated upstream
                dropped.append(tname)
            self._db.execute("DELETE FROM plugin_tables WHERE plugin_id = ?", (plugin_id,))
            self._db.execute(
                "DELETE FROM plugin_schema_migrations WHERE plugin_id = ?", (plugin_id,)
            )
            self._db.execute("DELETE FROM plugins WHERE plugin_id = ?", (plugin_id,))
        return dropped

    def upgrade(
        self,
        source_path: Path,
        *,
        install_deps: bool = True,
        reinstall_deps: bool = False,
    ) -> InstalledPlugin | UpgradeNoop:
        """Upgrade an existing plugin: apply only NEW migrations (S5).

        Version semantics (see distribution-and-plugin-install-design.md §7):
          - candidate == installed → return :class:`UpgradeNoop` (CLI exits 0)
          - candidate > installed  → run the upgrade
          - candidate < installed  → raise :class:`PluginInstallError`
            (downgrade is forbidden because migration rollback is not modeled)
        """
        source_path = source_path.resolve()
        meta = _load_metadata_yaml(source_path / "deeptrade_plugin.yaml")
        existing = self._fetch_one_plugin(meta.plugin_id)
        if existing is None:
            raise PluginNotFoundError(f"plugin not installed: {meta.plugin_id}")

        try:
            new_ver = Version(meta.version)
            cur_ver = Version(existing.version)
        except InvalidVersion as e:
            raise PluginInstallError(f"invalid version on {meta.plugin_id}: {e}") from e

        if new_ver == cur_ver:
            return UpgradeNoop(plugin_id=meta.plugin_id, version=existing.version)
        if new_ver < cur_ver:
            raise PluginInstallError(
                f"待装版本 {meta.version} 低于已装 {existing.version}; "
                f"如需降级，请先 `deeptrade plugin uninstall {meta.plugin_id} --purge`"
            )

        if meta.api_version not in SUPPORTED_API_VERSIONS:
            raise PluginInstallError(
                f"plugin api_version {meta.api_version!r} not supported by framework "
                f"(supported: {sorted(SUPPORTED_API_VERSIONS)})"
            )
        if meta.permissions.llm_tools is not False:
            raise PluginInstallError("permissions.llm_tools=true is forbidden")

        # Decide which migrations are new
        applied_checksums: dict[str, str] = {
            row[0]: row[1]
            for row in self._db.fetchall(
                "SELECT version, checksum FROM plugin_schema_migrations WHERE plugin_id = ?",
                (meta.plugin_id,),
            )
        }

        # T09 — every migration the plugin claims is *already applied* must
        # still carry the same checksum it carried at install time. A
        # silently-edited historical migration is treated as evidence the
        # plugin source has been tampered with: the on-disk schema reflects
        # the OLD migration body but the YAML now says the NEW body is what
        # was applied, so future upgrades / purges will reason about the
        # wrong state. Refuse the upgrade immediately.
        for mig in meta.migrations:
            stored = applied_checksums.get(mig.version)
            if stored is not None and stored != mig.checksum:
                raise PluginInstallError(
                    f"plugin {meta.plugin_id!r} migration {mig.version} "
                    f"checksum changed from {stored} to {mig.checksum}; "
                    f"refusing to upgrade — historical migrations are immutable"
                )

        new_migrations: list[tuple[MigrationSpec, str]] = []
        for mig in meta.migrations:
            if mig.version in applied_checksums:
                continue
            sql_text = _verify_migration_checksum(source_path, mig)
            new_migrations.append((mig, sql_text))

        # Copy new version
        target = self._install_root / meta.plugin_id / meta.version
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)

        # Resolve & install plugin deps BEFORE migrations / load. Failure
        # path mirrors install(): remove the copied dir, leave installed
        # deps in place (design §4.5).
        if install_deps:
            try:
                self._handle_dependencies(meta, reinstall=reinstall_deps)
            except DepInstallError as e:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                raise PluginInstallError(str(e)) from e

        # F-M5 — keep a backup of the previous install_path so we can roll back on failure
        prev_install_path = Path(existing.install_path)
        prev_metadata_yaml = self._db.fetchone(
            "SELECT metadata_yaml FROM plugins WHERE plugin_id = ?", (meta.plugin_id,)
        )

        try:
            with self._db.transaction():
                if new_migrations:
                    applied = self._apply_migrations(meta.plugin_id, meta, new_migrations)
                    self._record_migrations(meta.plugin_id, applied)
                # update the plugins row
                self._db.execute(
                    "UPDATE plugins SET name=?, version=?, type=?, api_version=?, entrypoint=?, "
                    "install_path=?, metadata_yaml=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE plugin_id=?",
                    (
                        meta.name,
                        meta.version,
                        meta.type,
                        meta.api_version,
                        meta.entrypoint,
                        str(target),
                        yaml.safe_dump(meta.model_dump(mode="json"), allow_unicode=True),
                        meta.plugin_id,
                    ),
                )
                # add any newly-declared tables to plugin_tables (idempotent)
                self._record_tables(meta)

                # F-M5 — same post-install validation as install():
                # missing-tables check + entrypoint import + validate_static
                missing = self._missing_declared_tables(meta)
                if missing:
                    raise PluginInstallError(
                        f"declared tables not created by migrations: {sorted(missing)}"
                    )
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

        # entrypoint + validate_static — outside the transaction (may load network-free
        # plugin code). Failure → roll back the plugins row to the previous version.
        try:
            instance = _load_entrypoint(target, meta.entrypoint, meta)
            validate = getattr(instance, "validate_static", None)
            if validate is not None:
                validate(_build_validate_ctx(self._db, meta))
        except Exception as e:
            # Roll back the plugins row to the prior version (install_path,
            # metadata_yaml, version, entrypoint). Do NOT touch migrations: the
            # new schema is already applied, and old metadata referenced an
            # earlier subset; rolling back schema would be more dangerous than
            # leaving forward-compatible columns/tables.
            if prev_metadata_yaml is not None:
                prev_meta = PluginMetadata.model_validate(yaml.safe_load(prev_metadata_yaml[0]))
                self._db.execute(
                    "UPDATE plugins SET name=?, version=?, api_version=?, entrypoint=?, "
                    "install_path=?, metadata_yaml=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE plugin_id=?",
                    (
                        prev_meta.name,
                        prev_meta.version,
                        prev_meta.api_version,
                        prev_meta.entrypoint,
                        str(prev_install_path),
                        prev_metadata_yaml[0],
                        meta.plugin_id,
                    ),
                )
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise PluginInstallError(
                f"upgrade validation failed for {meta.plugin_id}: {e}; rolled back to prior version"
            ) from e

        return self._compose_record(meta, target, enabled=existing.enabled)

    # --- dep handling ------------------------------------------------

    def _handle_dependencies(self, meta: PluginMetadata, *, reinstall: bool) -> None:
        """Resolve declared deps, fail loudly on conflict, run installer.

        Pre-conditions: ``meta.dependencies`` already passed Pydantic
        validation (PEP 508, no URL/VCS, unique names).
        """
        if not meta.dependencies:
            return

        specs = parse_specs(meta.dependencies)
        ownership = self._build_dep_ownership(exclude_plugin_id=meta.plugin_id)

        def attribute(canonical: str) -> str | None:
            return ownership.get(canonical)

        plan = plan_install(specs, attribute_conflict=attribute)

        if plan.conflicts:
            lines = [f"  - {c}" for c in plan.conflicts]
            raise DepInstallError(
                "plugin dependency conflicts:\n"
                + "\n".join(lines)
                + "\nResolve by uninstalling the conflicting plugin or "
                + "adjusting this plugin's specifier."
            )

        targets = specs if reinstall else plan.to_install
        if plan.skipped and not reinstall:
            logger.info(
                "plugin %s: %d dep(s) already satisfied: %s",
                meta.plugin_id,
                len(plan.skipped),
                [f"{r.name}=={v}" for r, v in plan.skipped],
            )
        run_install(targets, reinstall=reinstall)

    def _build_dep_ownership(self, *, exclude_plugin_id: str) -> dict[str, str]:
        """Map canonical package name → human-readable owner attribution.

        Sources, in priority order (first wins):
          1. Framework's own declared deps (``deeptrade-quant`` distribution).
          2. Already-installed plugins' declared ``dependencies``
             (looked up from ``plugins.metadata_yaml``).

        Used only for conflict messages; never gates install decisions.
        """
        out: dict[str, str] = {}

        # Framework deps
        try:
            dist = importlib_metadata.distribution("deeptrade-quant")
        except importlib_metadata.PackageNotFoundError:
            dist = None
        if dist is not None:
            for raw in dist.requires or []:
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    continue
                # Skip extras-only requirements (e.g. dev extras)
                if req.marker is not None:
                    try:
                        if not req.marker.evaluate({"extra": ""}):
                            continue
                    except Exception:  # noqa: BLE001 — marker eval edge cases
                        continue
                out.setdefault(canonicalize_name(req.name), "framework core dependency")

        # Other plugins
        try:
            rows = self._db.fetchall(
                "SELECT plugin_id, metadata_yaml FROM plugins WHERE plugin_id != ?",
                (exclude_plugin_id,),
            )
        except Exception:  # noqa: BLE001 — DB may be in transitional state
            rows = []
        for pid, meta_yaml in rows:
            try:
                meta_dict = yaml.safe_load(meta_yaml) or {}
            except yaml.YAMLError:
                continue
            for raw in meta_dict.get("dependencies", []) or []:
                try:
                    req = Requirement(raw)
                except InvalidRequirement:
                    continue
                out.setdefault(canonicalize_name(req.name), f"plugin {pid}")

        return out

    # --- internal helpers --------------------------------------------

    def _apply_migrations(
        self,
        plugin_id: str,
        meta: PluginMetadata,
        migs: Sequence[tuple[MigrationSpec, str]],
    ) -> list[tuple[MigrationSpec, list[str]]]:
        """Run each SQL inside the calling transaction with a per-migration
        schema-diff (v0.5 T05).

        For each migration:

        * Snapshot ``information_schema.tables`` ``before`` and ``after``.
        * Reject if any newly-created table is not listed in
          ``meta.tables`` — plugins cannot introduce undeclared tables.
        * Reject if any dropped table is in :data:`RESERVED_TABLE_NAMES` —
          plugins cannot drop framework tables even if they sneak past the
          metadata-time guard (T01).
        * Reject if any dropped table is not currently recorded as owned
          by this plugin in ``plugin_tables`` — plugins cannot drop tables
          owned by *other* plugins.

        Returns ``[(mig, sorted_added_tables), ...]`` so the caller can
        record the diff to ``plugin_schema_migrations.affected_tables``.
        Caller must wrap the call in a DB transaction so a rejected
        migration rolls back the partial SQL.
        """
        declared = {t.name for t in meta.tables}
        out: list[tuple[MigrationSpec, list[str]]] = []
        for mig, sql in migs:
            before = self._current_table_names()
            self._db.execute(sql)
            after = self._current_table_names()
            added = after - before
            removed = before - after

            undeclared = added - declared
            if undeclared:
                raise PluginInstallError(
                    f"migration {mig.version} created table(s) not declared in "
                    f"metadata.tables: {sorted(undeclared)}"
                )

            reserved_hit = removed & RESERVED_TABLE_NAMES
            if reserved_hit:
                raise PluginInstallError(
                    f"migration {mig.version} attempted to DROP framework-reserved "
                    f"table(s): {sorted(reserved_hit)}"
                )

            if removed:
                owned = {
                    r[0]
                    for r in self._db.fetchall(
                        "SELECT table_name FROM plugin_tables WHERE plugin_id = ?",
                        (plugin_id,),
                    )
                }
                foreign = removed - owned
                if foreign:
                    raise PluginInstallError(
                        f"migration {mig.version} attempted to DROP table(s) not "
                        f"owned by this plugin: {sorted(foreign)}"
                    )

            out.append((mig, sorted(added)))
        return out

    def _current_table_names(self) -> set[str]:
        return {
            r[0]
            for r in self._db.fetchall(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            )
        }

    def _record_plugin(self, meta: PluginMetadata, install_path: Path) -> None:
        self._db.execute(
            "INSERT INTO plugins(plugin_id, name, version, type, api_version, entrypoint, "
            "install_path, enabled, metadata_yaml) VALUES (?, ?, ?, ?, ?, ?, ?, TRUE, ?)",
            (
                meta.plugin_id,
                meta.name,
                meta.version,
                meta.type,
                meta.api_version,
                meta.entrypoint,
                str(install_path),
                yaml.safe_dump(meta.model_dump(mode="json"), allow_unicode=True),
            ),
        )

    def _record_tables(self, meta: PluginMetadata) -> None:
        for t in meta.tables:
            # idempotent: delete then insert
            self._db.execute(
                "DELETE FROM plugin_tables WHERE plugin_id = ? AND table_name = ?",
                (meta.plugin_id, t.name),
            )
            self._db.execute(
                "INSERT INTO plugin_tables(plugin_id, table_name, description, "
                "purge_on_uninstall) VALUES (?, ?, ?, ?)",
                (meta.plugin_id, t.name, t.description, t.purge_on_uninstall),
            )

    def _record_migrations(
        self,
        plugin_id: str,
        migs: Sequence[tuple[MigrationSpec, list[str]]],
    ) -> None:
        """Write a row per migration to ``plugin_schema_migrations``,
        including the JSON-serialized list of tables the migration created
        (v0.5 T05). ``affected_tables`` is set to ``NULL`` when the migration
        produced no new tables, so we can distinguish "nothing created" from
        "unknown (pre-v0.5 row)" — the latter is also NULL but originates
        from an older code path; downstream callers treat both alike."""
        for mig, affected in migs:
            affected_json: str | None = json.dumps(affected) if affected else None
            self._db.execute(
                "INSERT INTO plugin_schema_migrations(plugin_id, version, checksum, "
                "affected_tables) VALUES (?, ?, ?, ?)",
                (plugin_id, mig.version, mig.checksum, affected_json),
            )

    def _missing_declared_tables(self, meta: PluginMetadata) -> set[str]:
        existing = {
            r[0]
            for r in self._db.fetchall(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            )
        }
        declared = {t.name for t in meta.tables}
        return declared - existing

    def _fetch_one_plugin(self, plugin_id: str) -> InstalledPlugin | None:
        row = self._db.fetchone(
            "SELECT plugin_id, name, version, type, api_version, entrypoint, "
            "install_path, enabled, metadata_yaml FROM plugins WHERE plugin_id = ?",
            (plugin_id,),
        )
        if row is None:
            return None
        return self._row_to_record(row)

    def _row_to_record(self, row: Sequence[Any]) -> InstalledPlugin:
        meta_dict = yaml.safe_load(row[8])
        meta = PluginMetadata.model_validate(meta_dict)
        return InstalledPlugin(
            plugin_id=row[0],
            name=row[1],
            version=row[2],
            type=row[3],
            api_version=row[4],
            entrypoint=row[5],
            install_path=row[6],
            enabled=bool(row[7]),
            metadata=meta,
        )

    def _compose_record(
        self, meta: PluginMetadata, install_path: Path, *, enabled: bool
    ) -> InstalledPlugin:
        return InstalledPlugin(
            plugin_id=meta.plugin_id,
            name=meta.name,
            version=meta.version,
            type=meta.type,
            api_version=meta.api_version,
            entrypoint=meta.entrypoint,
            install_path=str(install_path),
            enabled=enabled,
            metadata=meta,
        )


def summarize_for_install(meta: PluginMetadata, source_path: Path) -> str:
    """Render the install confirmation pre-flight summary (CLI only)."""
    lines = textwrap.dedent(
        f"""
        plugin_id    : {meta.plugin_id}
        名称         : {meta.name}
        版本         : {meta.version}
        类型         : {meta.type}
        entrypoint   : {meta.entrypoint}
        来源         : {source_path}
        必要 API     : {", ".join(meta.permissions.tushare_apis.required) or "（无）"}
        可选 API     : {", ".join(meta.permissions.tushare_apis.optional) or "（无）"}
        迁移         : {", ".join(m.version for m in meta.migrations)}
        声明表 ({len(meta.tables)})  : {", ".join(t.name for t in meta.tables)}
        依赖         : {", ".join(meta.dependencies) or "（无）"}
        """
    ).strip()
    return lines
