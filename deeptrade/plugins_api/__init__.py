"""Public plugin API.

All plugins import from this package; the rest of ``deeptrade.*`` is internal.

Stable surface (api_version = "1"):
    - Plugin (Protocol), PluginContext — every plugin's contract: metadata + validate_static + dispatch
    - PluginMetadata, TableSpec, MigrationSpec, PluginPermissions, ...
    - StageProfile — LLM 调参档；插件持有 preset → stage 映射表，自行解析
"""

from __future__ import annotations

from deeptrade.plugins_api.base import Plugin, PluginContext
from deeptrade.plugins_api.llm import StageProfile
from deeptrade.plugins_api.metadata import (
    MigrationSpec,
    PluginMetadata,
    PluginPermissions,
    TableSpec,
    TushareApiPermissions,
)

__all__ = [
    "MigrationSpec",
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "PluginPermissions",
    "StageProfile",
    "TableSpec",
    "TushareApiPermissions",
]
