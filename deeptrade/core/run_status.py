"""Run status enum + Pydantic-layer validation.

DESIGN §13.1 status values + S3 fix: validation moved out of the DDL because
DuckDB doesn't ALTER CHECK constraints in-place.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Allowed values for a plugin run's terminal status.

    v0.5+: each plugin owns its own ``<prefix>_runs.status`` column (e.g.
    ``lub_runs`` / ``va_runs``); the framework no longer keeps a unified
    ``strategy_runs`` table.
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"
    CANCELLED = "cancelled"


def validate_status(value: str) -> RunStatus:
    """Validate a status string. Raises ValueError on invalid value."""
    return RunStatus(value)
