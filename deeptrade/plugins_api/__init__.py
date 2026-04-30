"""Public plugin API.

All plugins import from this package; the rest of ``deeptrade.*`` is internal.

Stable surface (api_version = "1"):
    - Plugin (Protocol) — every plugin's contract: metadata + validate_static + dispatch
    - ChannelPlugin (Protocol), PluginContext — channel-specific extension
    - NotificationPayload, NotificationSection, NotificationItem — notify data contract
    - PluginMetadata, TableSpec, MigrationSpec, PluginPermissions, ...
"""

from __future__ import annotations

from deeptrade.plugins_api.base import Plugin
from deeptrade.plugins_api.channel import ChannelPlugin, PluginContext
from deeptrade.plugins_api.metadata import (
    MigrationSpec,
    PluginMetadata,
    PluginPermissions,
    TableSpec,
    TushareApiPermissions,
)
from deeptrade.plugins_api.notify import (
    NotificationItem,
    NotificationPayload,
    NotificationSection,
)

__all__ = [
    "ChannelPlugin",
    "MigrationSpec",
    "NotificationItem",
    "NotificationPayload",
    "NotificationSection",
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "PluginPermissions",
    "TableSpec",
    "TushareApiPermissions",
]
