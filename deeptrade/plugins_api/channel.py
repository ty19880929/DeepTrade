"""ChannelPlugin contract — api_version "1".

A notification channel is a Python class implementing this Protocol. Channels
are loaded by the framework via the dotted entrypoint declared in their YAML
metadata, just like any other plugin.

Channel plugins are EXPLICITLY EXEMPTED from the "no direct external API" rule
that applies to other plugins: a channel IS an HTTP client by nature, so it may
``import httpx``/``requests`` directly. They still go through ``PluginContext``
for DB and config access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from deeptrade.plugins_api.base import Plugin

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.config import ConfigService
    from deeptrade.core.db import Database
    from deeptrade.plugins_api.metadata import PluginMetadata
    from deeptrade.plugins_api.notify import NotificationPayload


@dataclass
class PluginContext:
    """Minimal services bundle the framework hands to a plugin's
    ``validate_static`` (during install) and to a channel plugin's ``push``
    (during notify).

    Plugins that need richer services (TushareClient, LLMManager / LLMClient, etc.)
    construct them inside their own ``dispatch`` from these primitives.
    """

    db: Database
    config: ConfigService
    plugin_id: str | None = None


@runtime_checkable
class ChannelPlugin(Plugin, Protocol):
    """A plugin that can also receive ``NotificationPayload`` from the
    framework's notifier (in addition to providing the standard CLI dispatch).

    Implementations satisfy both the :class:`Plugin` contract (``metadata`` +
    ``validate_static`` + ``dispatch``) AND the channel-specific ``push`` hook.
    """

    metadata: PluginMetadata

    def push(self, ctx: PluginContext, payload: NotificationPayload) -> None:
        """Format payload and dispatch to the IM platform.

        May raise — the framework's MultiplexNotifier catches and isolates
        per-channel failures so one broken channel never blocks others.
        """
