"""Strategy event model.

DESIGN §8.5: full enumeration of EventType values. v0.5+: each plugin owns
its own ``<prefix>_events`` table (e.g. ``lub_events`` / ``va_events``) and
decides whether/how to persist these — the framework no longer provides a
unified ``strategy_events`` table or a runner that writes into one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    # step lifecycle
    STEP_STARTED = "step.started"
    STEP_PROGRESS = "step.progress"
    STEP_FINISHED = "step.finished"
    # data sync
    DATA_SYNC_STARTED = "data.sync.started"
    DATA_SYNC_FINISHED = "data.sync.finished"
    # tushare
    TUSHARE_CALL = "tushare.call"
    TUSHARE_FALLBACK = "tushare.fallback"
    TUSHARE_UNAUTH = "tushare.unauthorized"
    # llm
    LLM_BATCH_STARTED = "llm.batch.started"
    LLM_BATCH_FINISHED = "llm.batch.finished"
    LLM_FINAL_RANK = "llm.final_ranking"
    VALIDATION_FAILED = "validation.failed"
    # result
    RESULT_PERSISTED = "result.persisted"
    LOG = "log"
    # Live row content — plugin-driven dashboard "current phase" message.
    # The dashboard's Live row reads `event.message` of LIVE_STATUS events
    # verbatim and shows nothing else there. The framework deliberately does
    # NOT parse any other event's message string to infer phase, so plugin
    # wording stays decoupled from framework code. Optional payload key
    # ``terminal=True`` marks the emit as the run's final-state text so
    # the framework's mark_finished default doesn't overwrite it.
    LIVE_STATUS = "live.status"


class EventLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class StrategyEvent(BaseModel):
    """Single event emitted by a strategy plugin."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    level: EventLevel = EventLevel.INFO
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
