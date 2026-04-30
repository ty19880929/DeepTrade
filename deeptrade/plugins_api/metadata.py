"""Plugin metadata schema.

DESIGN §8.2 (v0.3.1):
    * `tables` declares table names + purge policy ONLY (no inline DDL).
    * `migrations` is the SOLE DDL execution path (S1 fix).
    * `permissions.llm_tools` is fixed False (M3 hard constraint).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class TushareApiPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


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
    # v0.4: widened to support channel plugins (DESIGN §18.4). Old yaml with
    # type='strategy' (or omitted) remains valid via the default.
    type: Literal["strategy", "channel"] = "strategy"
    api_version: str
    entrypoint: str = Field(..., pattern=r"^[A-Za-z_][\w\.]*:[A-Za-z_]\w*$")
    description: str
    author: str = ""
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    tables: list[TableSpec]
    migrations: list[MigrationSpec]

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
