"""LubRuntime — context bundle the plugin's pipeline runs against.

Replaces the old framework-provided ``StrategyContext``. The plugin owns its
own runtime now: it constructs db / config / tushare itself, and obtains LLM
clients on-demand from the framework's :class:`LLMManager`.

v0.6 — ``llm: DeepSeekClient`` field removed. ``llms: LLMManager`` is the
new framework hand-off; runner / pipeline pull a per-provider ``LLMClient``
via ``rt.llms.get_client(name, plugin_id=, run_id=)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deeptrade.plugins_api.events import EventLevel, EventType, StrategyEvent

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.config import ConfigService
    from deeptrade.core.db import Database
    from deeptrade.core.llm_manager import LLMManager
    from deeptrade.core.tushare_client import TushareClient
    from deeptrade.plugins_api.notify import NotificationPayload

logger = logging.getLogger(__name__)

PLUGIN_ID = "limit-up-board"


@dataclass
class LubRuntime:
    """Services bundle the plugin's run() / sync_data() use.

    ``llms`` is the framework's LLMManager — call
    ``rt.llms.get_client(name, plugin_id=rt.plugin_id, run_id=rt.run_id, ...)``
    to obtain a per-provider client. The plugin may use multiple providers
    in the same run.
    """

    db: Database
    config: ConfigService
    llms: LLMManager
    plugin_id: str = PLUGIN_ID
    run_id: str | None = None
    is_intraday: bool = False
    tushare: TushareClient | None = None

    def emit(
        self,
        event_type: EventType,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        **payload: object,
    ) -> StrategyEvent:
        return StrategyEvent(type=event_type, level=level, message=message, payload=dict(payload))

    def notify(self, payload: NotificationPayload) -> bool:
        """Push a NotificationPayload through the framework's notifier.

        Returns True on success, False if no channel is enabled or dispatch
        raised. Never blocks on HTTP — top-level ``deeptrade.notify`` builds
        a notifier that uses an async dispatch worker.
        """
        from deeptrade import notify as _notify

        try:
            _notify(self.db, payload)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("notify dispatch failed: %s", e)
            return False

    def is_notify_enabled(self) -> bool:
        """Cheap probe: any channel plugin enabled?"""
        from deeptrade.core.plugin_manager import PluginManager

        mgr = PluginManager(self.db)
        return any(r.type == "channel" and r.enabled for r in mgr.list_all())


def build_tushare_client(
    rt: LubRuntime,
    *,
    intraday: bool = False,
    event_cb: Any = None,
) -> TushareClient:
    """Construct a TushareClient bound to this plugin."""
    from deeptrade.core.tushare_client import TushareClient, TushareSDKTransport

    token = rt.config.get("tushare.token")
    if not token:
        raise RuntimeError("tushare.token not configured; run `deeptrade config set-tushare`")
    cfg = rt.config.get_app_config()
    transport = TushareSDKTransport(str(token))
    return TushareClient(
        rt.db,
        transport,
        plugin_id=rt.plugin_id,
        rps=cfg.tushare_rps,
        intraday=intraday,
        event_cb=event_cb,
    )


def pick_llm_provider(rt: LubRuntime) -> str:
    """Pick which configured LLM provider to use for this run.

    v0.6 policy: prefer ``deepseek`` (the original default and the target of
    the legacy-config auto-migration), else fall back to the first available
    provider. v0.7 will let the user override via a plugin-level config key
    such as ``limit-up-board.default_llm``.

    Raises ``RuntimeError`` if no provider is configured at all.
    """
    available = rt.llms.list_providers()
    if not available:
        raise RuntimeError(
            "No LLM provider configured; run `deeptrade config set-llm`"
        )
    if "deepseek" in available:
        return "deepseek"
    return available[0]
