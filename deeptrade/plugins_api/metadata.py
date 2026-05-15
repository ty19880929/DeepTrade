"""Plugin metadata schema.

DESIGN §8.2 (v0.3.1):
    * `tables` declares table names + purge policy ONLY (no inline DDL).
    * `migrations` is the SOLE DDL execution path (S1 fix).
    * `permissions.llm_tools` is fixed False (M3 hard constraint).
DESIGN v0.4 (plugin dependency management):
    * `dependencies` declares third-party Python packages the plugin needs.
      PEP 508 specifier strings; framework installs them at plugin install /
      upgrade time. See ``CHANGELOG.md`` v0.4.0.
DESIGN v0.5 (plugin trust boundary):
    * Plugin-declared tables must not collide with framework-owned table
      names (:data:`RESERVED_TABLE_NAMES`).
    * Entrypoint's top-level package must equal ``plugin_id.replace('-','_')``
      and must not collide with framework / common-dependency packages
      (:data:`RESERVED_TOP_PACKAGES`).
DESIGN v0.6:
    * ``table_prefix`` declares the namespace the plugin's tables live in;
      v0.5 emitted a ``DeprecationWarning`` when tables fell outside the
      derived prefix and the prefix was omitted — v0.6 promotes that to a
      hard error so the lifecycle invariant is enforced uniformly.
"""

from __future__ import annotations

from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Framework-owned tables; must NEVER be claimed or dropped by a plugin.
# Kept in sync with ``deeptrade/core/migrations/core/20260509_001_init.sql``
# plus later core migrations (``tushare_cache_blob`` was added in v0.4.0).
RESERVED_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "app_config",
        "secret_store",
        "schema_migrations",
        "plugins",
        "plugin_tables",
        "plugin_schema_migrations",
        "llm_calls",
        "tushare_calls",
        "tushare_sync_state",
        "tushare_cache_blob",
    }
)

# Top-level Python package names a plugin entrypoint must NEVER claim.
# Includes the framework's own package and the common dependencies it ships
# with — a plugin shipping a directory named ``deeptrade/`` or ``pydantic/``
# would replace the cached framework copy at import time and corrupt the
# runtime (see ``plugin_manager._load_entrypoint`` sys.modules eviction).
RESERVED_TOP_PACKAGES: frozenset[str] = frozenset(
    {
        "deeptrade",
        "tests",
        "click",
        "typer",
        "openai",
        "pandas",
        "duckdb",
        "pydantic",
        "packaging",
        "yaml",
        "keyring",
        "questionary",
        "rich",
        "tushare",
        "tenacity",
    }
)


class TableSpec(BaseModel):
    """Table the plugin owns. DDL is in migrations/, NOT here."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = ""
    purge_on_uninstall: bool = True


class MigrationSpec(BaseModel):
    """One SQL migration file. Filename version pattern: 'YYYYMMDD_NNN'."""

    model_config = ConfigDict(extra="forbid")
    version: str = Field(..., pattern=r"^\d{8}_\d{3,}$")
    file: str
    checksum: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")


_CACHE_CLASS_VALUES: frozenset[str] = frozenset(
    {"static", "trade_day_immutable", "trade_day_mutable", "hot_or_anns"}
)


class TushareApiPermissions(BaseModel):
    """Per-plugin Tushare API permissions + cache hints.

    v0.6 — ``cache_overrides`` lets plugin authors declare a non-default
    cache class for any API. Without this, ``TushareClient`` defaults unknown
    API names to ``trade_day_immutable`` (history-data sized for the typical
    quant workload); a plugin pulling an intraday-mutable API can now override
    the default safely via the metadata rather than monkey-patching the
    framework's classifier table.
    """

    model_config = ConfigDict(extra="forbid")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    cache_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cache_overrides_values_valid(self) -> TushareApiPermissions:
        """Each override value must name one of the four supported cache
        classes — typos here would silently break caching at runtime."""
        bad = sorted(
            f"{api}={cls!r}"
            for api, cls in self.cache_overrides.items()
            if cls not in _CACHE_CLASS_VALUES
        )
        if bad:
            raise ValueError(
                f"permissions.tushare_apis.cache_overrides has invalid class(es): "
                f"{bad}; allowed: {sorted(_CACHE_CLASS_VALUES)}"
            )
        return self


class PluginPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tushare_apis: TushareApiPermissions = Field(default_factory=TushareApiPermissions)
    llm: bool = False
    # M3 hard constraint: any non-False value is rejected at install time.
    llm_tools: Literal[False] = False


class PluginMetadata(BaseModel):
    """Top-level plugin descriptor parsed from deeptrade_plugin.yaml."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., pattern=r"^[a-z][a-z0-9-]{2,31}$")
    name: str
    version: str
    type: Literal["strategy"] = "strategy"
    api_version: str
    entrypoint: str = Field(..., pattern=r"^[A-Za-z_][\w\.]*:[A-Za-z_]\w*$")
    description: str
    author: str = ""
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    tables: list[TableSpec]
    migrations: list[MigrationSpec]
    dependencies: list[str] = Field(default_factory=list)
    # v0.5: declared namespace for plugin-owned tables. When set, all entries
    # in ``tables`` must start with it. When unset, the framework derives a
    # default of ``plugin_id.replace('-', '_') + '_'`` and only warns on
    # mismatch; v0.6 will promote that warning to a hard error.
    table_prefix: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,15}_$")

    @model_validator(mode="after")
    def _dependencies_valid(self) -> PluginMetadata:
        """Each entry must parse as PEP 508 ``Requirement``; no VCS/URL forms;
        no duplicate package names (canonicalized)."""
        seen: dict[str, str] = {}
        for raw in self.dependencies:
            try:
                req = Requirement(raw)
            except InvalidRequirement as e:
                raise ValueError(f"invalid dependency spec {raw!r}: {e}") from e
            if req.url:
                raise ValueError(
                    f"dependency {raw!r}: VCS/URL forms (git+https://, package @ url) "
                    f"are not allowed; use PEP 508 version specifiers only"
                )
            canonical = canonicalize_name(req.name)
            if canonical in seen:
                raise ValueError(
                    f"duplicate dependency package {req.name!r} "
                    f"(also declared as {seen[canonical]!r}); combine into a single spec"
                )
            seen[canonical] = raw
        return self

    @model_validator(mode="after")
    def _migrations_not_empty(self) -> PluginMetadata:
        if not self.migrations:
            raise ValueError(
                "metadata.migrations cannot be empty; DDL must be managed via migrations/*.sql"
            )
        # version uniqueness
        versions = [m.version for m in self.migrations]
        if len(versions) != len(set(versions)):
            raise ValueError("duplicate migration versions in metadata.migrations")
        return self

    @model_validator(mode="after")
    def _tables_unique(self) -> PluginMetadata:
        names = [t.name for t in self.tables]
        if len(names) != len(set(names)):
            raise ValueError("duplicate table names in metadata.tables")
        return self

    @model_validator(mode="after")
    def _tables_not_reserved(self) -> PluginMetadata:
        """v0.5 T01: reject plugins that try to own a framework-managed table.

        Without this, ``uninstall --purge`` would happily DROP ``app_config``
        / ``plugins`` / ``llm_calls`` etc. when ``plugin_tables`` listed them.
        """
        collisions = sorted({t.name for t in self.tables} & RESERVED_TABLE_NAMES)
        if collisions:
            raise ValueError(
                f"metadata.tables claims framework-reserved table name(s) "
                f"{collisions}; reserved set: {sorted(RESERVED_TABLE_NAMES)}"
            )
        return self

    @model_validator(mode="after")
    def _tables_match_prefix(self) -> PluginMetadata:
        """v0.6 T02 (hard-fail): enforce a per-plugin table namespace.

        * ``table_prefix`` declared explicitly → all declared tables MUST
          start with it.
        * ``table_prefix`` omitted → derive ``plugin_id.replace('-','_') + '_'``
          and require all declared tables to match.

        v0.5 emitted a ``DeprecationWarning`` for the omitted-prefix path;
        v0.6 promotes it to a hard error per the v0.5 plan §5. Plugins that
        hit this need to either rename their tables to share a common prefix
        or declare an explicit ``table_prefix`` in ``deeptrade_plugin.yaml``.
        """
        if self.table_prefix is not None:
            offenders = sorted(
                t.name for t in self.tables if not t.name.startswith(self.table_prefix)
            )
            if offenders:
                raise ValueError(
                    f"metadata.tables {offenders} do not start with declared "
                    f"table_prefix {self.table_prefix!r}"
                )
            return self

        derived = self.plugin_id.replace("-", "_") + "_"
        offenders = sorted(t.name for t in self.tables if not t.name.startswith(derived))
        if offenders:
            raise ValueError(
                f"plugin {self.plugin_id!r}: tables {offenders} do not match the "
                f"derived table prefix {derived!r}. Rename the tables or declare "
                f"an explicit `table_prefix` in deeptrade_plugin.yaml."
            )
        return self

    @model_validator(mode="after")
    def _entrypoint_top_pkg_matches_plugin_id(self) -> PluginMetadata:
        """v0.5 T04: entrypoint's top-level package must match the plugin_id
        (with ``-`` → ``_``) and must NOT collide with framework / common
        dependency packages.

        Without this, a plugin can declare ``entrypoint: deeptrade.x:Y`` and
        cause ``_load_entrypoint`` to evict the cached framework module while
        searching for the plugin module — a foot-gun that became visible in
        the v0.5 review.
        """
        module_path = self.entrypoint.split(":", 1)[0]
        top_pkg = module_path.split(".", 1)[0]
        expected = self.plugin_id.replace("-", "_")
        if top_pkg != expected:
            raise ValueError(
                f"entrypoint top-level package {top_pkg!r} does not match "
                f"plugin_id-derived package {expected!r} "
                f"(plugin_id={self.plugin_id!r})"
            )
        if top_pkg in RESERVED_TOP_PACKAGES:
            raise ValueError(
                f"entrypoint top-level package {top_pkg!r} is reserved by the "
                f"framework or its dependencies; reserved set: "
                f"{sorted(RESERVED_TOP_PACKAGES)}"
            )
        return self
