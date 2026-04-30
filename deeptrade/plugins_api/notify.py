"""IM notification payload — strategy plugin → notifier contract.

DESIGN §18.3: payload carries STRUCTURED SEMANTIC DATA only (no pre-rendered
markdown). Channel plugins are responsible for format conversion (markdown for
feishu/dingtalk, plain text for SMS, interactive blocks for slack, ...).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from deeptrade.core.run_status import RunStatus


class NotificationItem(BaseModel):
    """One semantic row (a stock pick, a watchlist entry, ...).

    Channels render this as a list row, card field, or a bullet — whichever
    fits their format. ``fields`` carries channel-agnostic extras the channel
    may surface if it has the space.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str | None = None
    rank: int | None = None
    score: float | None = None
    label: str | None = None
    note: str | None = None
    fields: dict[str, str | int | float] = Field(default_factory=dict)


class NotificationSection(BaseModel):
    """A semantically related group of items (e.g. 'top_candidates' / 'watchlist').

    ``key`` is the stable machine-readable identifier; ``title`` is what
    channels display.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    items: list[NotificationItem]


class NotificationPayload(BaseModel):
    """Everything a strategy plugin hands to the notifier in one shot.

    Channels MUST tolerate empty ``sections`` / ``metrics`` (degraded modes
    like SMS only get ``title`` + ``summary``).
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    run_id: str
    status: RunStatus
    title: str
    summary: str
    sections: list[NotificationSection] = Field(default_factory=list)
    metrics: dict[str, str | int | float] = Field(default_factory=dict)
    report_dir: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
