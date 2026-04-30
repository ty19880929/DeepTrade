"""NotificationPayload / Section / Item Pydantic schema tests.

DESIGN §18.3 — payload is structured semantic data, not pre-rendered markdown.
The schema rejects unknown fields (extra='forbid') so accidental field name
drift between plugin authors and channel implementations is caught early.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deeptrade.core.run_status import RunStatus
from deeptrade.plugins_api.notify import (
    NotificationItem,
    NotificationPayload,
    NotificationSection,
)


def _minimal_payload() -> NotificationPayload:
    return NotificationPayload(
        plugin_id="x", run_id="r", status=RunStatus.SUCCESS, title="t", summary="s"
    )


def test_minimal_payload_valid() -> None:
    p = _minimal_payload()
    assert p.sections == []
    assert p.metrics == {}
    assert p.report_dir is None
    assert p.extras == {}


def test_payload_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        NotificationPayload(
            plugin_id="x",
            run_id="r",
            status=RunStatus.SUCCESS,
            title="t",
            summary="s",
            markdown="should not exist",  # type: ignore[call-arg]
        )


def test_section_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        NotificationSection(key="k", title="t", items=[], extra="no")  # type: ignore[call-arg]


def test_item_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        NotificationItem(code="c", weird="no")  # type: ignore[call-arg]


def test_full_payload_with_sections_and_metrics() -> None:
    p = NotificationPayload(
        plugin_id="lub",
        run_id="abc",
        status=RunStatus.PARTIAL_FAILED,
        title="title",
        summary="sum",
        sections=[
            NotificationSection(
                key="top",
                title="Top",
                items=[
                    NotificationItem(
                        code="600519.SH", name="贵州茅台", rank=1, score=87.5,
                        label="top_candidate", note="note",
                        fields={"sector": "白酒", "limit_streak": 3},
                    ),
                ],
            ),
        ],
        metrics={"selected": 5, "candidates": 50, "trade_date": "20260428"},
        report_dir="/tmp/abc",
        extras={"source": "test"},
    )
    assert p.sections[0].items[0].code == "600519.SH"
    assert p.metrics["selected"] == 5
    # round-trip via Pydantic dump preserves structure
    d = p.model_dump(mode="json")
    p2 = NotificationPayload.model_validate(d)
    assert p2 == p


def test_status_enum_accepted_strings() -> None:
    """RunStatus is a StrEnum so callers may pass either enum or its string."""
    p = NotificationPayload(
        plugin_id="x", run_id="r", status="success", title="t", summary="s"  # type: ignore[arg-type]
    )
    assert p.status == RunStatus.SUCCESS
