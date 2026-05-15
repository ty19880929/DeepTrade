"""Public plugin API.

All plugins import from this package; the rest of ``deeptrade.*`` is internal.

Stable surface:

* Contract:        Plugin (Protocol), PluginContext (api_version "1" and "2").
* Metadata schema: PluginMetadata, TableSpec, MigrationSpec, PluginPermissions,
                   TushareApiPermissions.
* LLM profile:     StageProfile — plugin-owned preset → stage mapping.
* Services:        LLMManager, TushareClient — construct directly from
                   :class:`PluginContext` primitives. v0.6+ promotes these
                   to the public surface (was: ``deeptrade.core.*`` internals).
* Errors:          render_exception — DEEPTRADE_DEBUG-aware formatter for
                   dispatch tails; debug_enabled — env-flag probe.
"""

from __future__ import annotations

from deeptrade.core.llm_manager import LLMManager
from deeptrade.core.tushare_client import TushareClient
from deeptrade.plugins_api.base import Plugin, PluginContext
from deeptrade.plugins_api.errors import debug_enabled, render_exception
from deeptrade.plugins_api.llm import StageProfile
from deeptrade.plugins_api.metadata import (
    MigrationSpec,
    PluginMetadata,
    PluginPermissions,
    TableSpec,
    TushareApiPermissions,
)

__all__ = [
    "LLMManager",
    "MigrationSpec",
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "PluginPermissions",
    "StageProfile",
    "TableSpec",
    "TushareApiPermissions",
    "TushareClient",
    "debug_enabled",
    "render_exception",
]
