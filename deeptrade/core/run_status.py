"""Run status enum + Pydantic-layer validation.

DESIGN §13.1 status values + S3 fix: validation moved out of the DDL because
DuckDB doesn't ALTER CHECK constraints in-place.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Allowed values for strategy_runs.status."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"
    CANCELLED = "cancelled"


def validate_status(value: str) -> RunStatus:
    """Validate a status string. Raises ValueError on invalid value."""
    return RunStatus(value)
