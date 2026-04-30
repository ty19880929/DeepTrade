"""VaRuntime — context bundle the volume-anomaly plugin's pipeline runs against."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deeptrade.plugins_api.events import EventLevel, EventType, StrategyEvent

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.config import ConfigService
    from deeptrade.core.db import Database
    from deeptrade.core.deepseek_client import DeepSeekClient
    from deeptrade.core.tushare_client import TushareClient
    from deeptrade.plugins_api.notify import NotificationPayload

logger = logging.getLogger(__name__)

PLUGIN_ID = "volume-anomaly"


@dataclass
class VaRuntime:
    db: Database
    config: ConfigService
    plugin_id: str = PLUGIN_ID
    run_id: str | None = None
    is_intraday: bool = False
    tushare: TushareClient | None = None
    llm: DeepSeekClient | None = None

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
        from deeptrade import notify as _notify

        try:
            _notify(self.db, payload)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("notify dispatch failed: %s", e)
            return False


def build_tushare_client(rt: VaRuntime, *, intraday: bool = False, event_cb: Any = None):
    from deeptrade.core.tushare_client import TushareClient, TushareSDKTransport

    token = rt.config.get("tushare.token")
    if not token:
        raise RuntimeError("tushare.token not configured; run `deeptrade config set-tushare`")
    cfg = rt.config.get_app_config()
    return TushareClient(
        rt.db,
        TushareSDKTransport(str(token)),
        plugin_id=rt.plugin_id,
        rps=cfg.tushare_rps,
        intraday=intraday,
        event_cb=event_cb,
    )


def build_llm_client(rt: VaRuntime):
    from pathlib import Path

    from deeptrade.core import paths
    from deeptrade.core.deepseek_client import DeepSeekClient, OpenAIClientTransport

    api_key = rt.config.get("deepseek.api_key")
    if not api_key:
        raise RuntimeError("deepseek.api_key not configured; run `deeptrade config set-deepseek`")
    cfg = rt.config.get_app_config()
    profiles = rt.config.get_profile()
    transport = OpenAIClientTransport(
        api_key=str(api_key),
        base_url=cfg.deepseek_base_url,
        timeout=cfg.deepseek_timeout,
    )
    reports_dir: Path | None = paths.reports_dir() / rt.run_id if rt.run_id else None
    return DeepSeekClient(
        rt.db,
        transport,
        model=cfg.deepseek_model,
        profiles=profiles,
        plugin_id=rt.plugin_id,
        run_id=rt.run_id,
        audit_full_payload=cfg.deepseek_audit_full_payload,
        reports_dir=reports_dir,
    )
