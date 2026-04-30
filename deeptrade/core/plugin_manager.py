"""Plugin install / validate / uninstall / upgrade.

DESIGN §8.3 + S1 (migrations are the sole DDL source) + S2 (install never
touches the network) + M3 (llm_tools=true is rejected).
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import shutil
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from deeptrade.core import paths
from deeptrade.core.db import Database
from deeptrade.plugins_api.base import Plugin
from deeptrade.plugins_api.metadata import MigrationSpec, PluginMetadata

logger = logging.getLogger(__name__)

CURRENT_API_VERSION = "1"

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

    if not hasattr(module, class_name):
        raise PluginInstallError(f"{module_path} has no class {class_name}")
    plugin_cls = getattr(module, class_name)
    instance = plugin_cls()
    if metadata is not None:
        instance.metadata = metadata
    return instance  # type: ignore[no-any-return]


def _build_validate_ctx(db: Database, meta: PluginMetadata) -> Any:
    """Build the framework's minimal ``PluginContext`` for ``validate_static``.

    All plugin types (strategy / channel / future) share the same narrow
    context shape: db + config + plugin_id. Plugins that need richer services
    (TushareClient, DeepSeekClient, ...) construct them inside their own
    ``dispatch`` from these primitives.
    """
    from deeptrade.core.config import ConfigService
    from deeptrade.plugins_api.channel import PluginContext

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

    def install(self, source_path: Path) -> InstalledPlugin:
        """Install a plugin from a local directory. Network never touched."""
        source_path = source_path.resolve()
        if not source_path.is_dir():
            raise PluginInstallError(f"source path is not a directory: {source_path}")

        meta = _load_metadata_yaml(source_path / "deeptrade_plugin.yaml")

        if meta.plugin_id in RESERVED_PLUGIN_IDS:
            raise PluginInstallError(
                f"plugin_id {meta.plugin_id!r} is reserved by the framework "
                f"(reserved: {sorted(RESERVED_PLUGIN_IDS)})"
            )

        if meta.api_version != CURRENT_API_VERSION:
            raise PluginInstallError(
                f"plugin api_version {meta.api_version} != framework {CURRENT_API_VERSION}"
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

        # Apply migrations + write registries inside ONE transaction.
        try:
            with self._db.transaction():
                applied = self._apply_migrations(meta.plugin_id, mig_sql)
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
            if hasattr(instance, "validate_static"):
                instance.validate_static(_build_validate_ctx(self._db, meta))
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
            raise PluginNotFoundError(plugin_id)
        return rec

    def disable(self, plugin_id: str) -> None:
        if self._fetch_one_plugin(plugin_id) is None:
            raise PluginNotFoundError(plugin_id)
        self._db.execute("UPDATE plugins SET enabled = FALSE WHERE plugin_id = ?", (plugin_id,))

    def enable(self, plugin_id: str) -> None:
        rec = self._fetch_one_plugin(plugin_id)
        if rec is None:
            raise PluginNotFoundError(plugin_id)
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
            raise PluginNotFoundError(plugin_id)

        dropped: list[str] = []
        if purge:
            tables = self._db.fetchall(
                "SELECT table_name, purge_on_uninstall FROM plugin_tables WHERE plugin_id = ?",
                (plugin_id,),
            )
            with self._db.transaction():
                for tname, purge_flag in tables:
                    if purge_flag:
                        self._db.execute(f"DROP TABLE IF EXISTS {tname}")  # noqa: S608 — name validated by Pydantic regex
                        dropped.append(tname)
                self._db.execute("DELETE FROM plugin_tables WHERE plugin_id = ?", (plugin_id,))
                self._db.execute(
                    "DELETE FROM plugin_schema_migrations WHERE plugin_id = ?", (plugin_id,)
                )
                self._db.execute("DELETE FROM plugins WHERE plugin_id = ?", (plugin_id,))
        else:
            # default: just disable + remove the install copy
            self._db.execute("UPDATE plugins SET enabled = FALSE WHERE plugin_id = ?", (plugin_id,))

        # remove the on-disk install copy (idempotent)
        install_path = Path(rec.install_path)
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)

        return {"purged_tables": dropped, "purge": purge}

    def upgrade(self, source_path: Path) -> InstalledPlugin:
        """Upgrade an existing plugin: apply only NEW migrations (S5)."""
        source_path = source_path.resolve()
        meta = _load_metadata_yaml(source_path / "deeptrade_plugin.yaml")
        existing = self._fetch_one_plugin(meta.plugin_id)
        if existing is None:
            raise PluginNotFoundError(meta.plugin_id)

        if meta.api_version != CURRENT_API_VERSION:
            raise PluginInstallError(
                f"plugin api_version {meta.api_version} != framework {CURRENT_API_VERSION}"
            )
        if meta.permissions.llm_tools is not False:
            raise PluginInstallError("permissions.llm_tools=true is forbidden")

        # Decide which migrations are new
        applied_versions = {
            row[0]
            for row in self._db.fetchall(
                "SELECT version FROM plugin_schema_migrations WHERE plugin_id = ?",
                (meta.plugin_id,),
            )
        }
        new_migrations: list[tuple[MigrationSpec, str]] = []
        for mig in meta.migrations:
            if mig.version in applied_versions:
                continue
            sql_text = _verify_migration_checksum(source_path, mig)
            new_migrations.append((mig, sql_text))

        # Copy new version
        target = self._install_root / meta.plugin_id / meta.version
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_path, target)

        # F-M5 — keep a backup of the previous install_path so we can roll back on failure
        prev_install_path = Path(existing.install_path)
        prev_metadata_yaml = self._db.fetchone(
            "SELECT metadata_yaml FROM plugins WHERE plugin_id = ?", (meta.plugin_id,)
        )

        try:
            with self._db.transaction():
                if new_migrations:
                    self._apply_migrations(meta.plugin_id, new_migrations)
                    self._record_migrations(meta.plugin_id, new_migrations)
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
            if hasattr(instance, "validate_static"):
                instance.validate_static(_build_validate_ctx(self._db, meta))
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

    # --- internal helpers --------------------------------------------

    def _apply_migrations(
        self, plugin_id: str, migs: Sequence[tuple[MigrationSpec, str]]
    ) -> list[tuple[MigrationSpec, str]]:
        """Run each SQL inside the calling transaction. Caller wraps in transaction."""
        for _mig, sql in migs:
            # split on ';' is not safe for some DDL but DuckDB supports executing
            # multi-statement strings via ``execute`` with ``;``-separated bodies.
            for stmt in self._iter_statements(sql):
                if stmt.strip():
                    self._db.execute(stmt)
        return list(migs)

    @staticmethod
    def _iter_statements(sql: str) -> list[str]:
        """Split SQL on top-level semicolons. Handles -- comments and quoted strings."""
        stmts: list[str] = []
        buf: list[str] = []
        in_single = False
        in_double = False
        i = 0
        n = len(sql)
        while i < n:
            ch = sql[i]
            # line comment
            if not in_single and not in_double and ch == "-" and i + 1 < n and sql[i + 1] == "-":
                # consume to end of line
                eol = sql.find("\n", i)
                if eol == -1:
                    eol = n
                # don't include comment text in buffer
                i = eol
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            if ch == ";" and not in_single and not in_double:
                stmts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1
        if buf:
            tail = "".join(buf).strip()
            if tail:
                stmts.append(tail)
        return [s.strip() for s in stmts if s.strip()]

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

    def _record_migrations(self, plugin_id: str, migs: Sequence[tuple[MigrationSpec, str]]) -> None:
        for mig, _ in migs:
            self._db.execute(
                "INSERT INTO plugin_schema_migrations(plugin_id, version, checksum) "
                "VALUES (?, ?, ?)",
                (plugin_id, mig.version, mig.checksum),
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
        plugin_id  : {meta.plugin_id}
        name       : {meta.name}
        version    : {meta.version}
        type       : {meta.type}
        entrypoint : {meta.entrypoint}
        source     : {source_path}
        required   : {", ".join(meta.permissions.tushare_apis.required) or "(none)"}
        optional   : {", ".join(meta.permissions.tushare_apis.optional) or "(none)"}
        migrations : {", ".join(m.version for m in meta.migrations)}
        tables ({len(meta.tables)}): {", ".join(t.name for t in meta.tables)}
        """
    ).strip()
    return lines
