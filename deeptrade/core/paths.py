"""Resolve well-known paths for DeepTrade local artifacts.

DESIGN §5.1 user directory layout.

Override the root via the DEEPTRADE_HOME env var (used by tests for isolation).
"""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    """Root of all local artifacts. Defaults to ~/.deeptrade."""
    override = os.environ.get("DEEPTRADE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".deeptrade"


def db_path() -> Path:
    """Path to the main DuckDB file."""
    override = os.environ.get("DEEPTRADE_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return home_dir() / "deeptrade.duckdb"


def logs_dir() -> Path:
    return home_dir() / "logs"


def reports_dir() -> Path:
    return home_dir() / "reports"


def plugins_dir() -> Path:
    return home_dir() / "plugins" / "installed"


def plugins_cache_dir() -> Path:
    return home_dir() / "plugins" / "cache"


def dep_snapshots_dir() -> Path:
    """v0.7 H6-b — pre-install ``pip list --format=freeze`` baselines
    keyed by plugin_id. Each plugin install / upgrade adds one
    ``pre-install-<UTC>.txt`` snapshot here so users can diff back to
    the working state when a botched install leaves the env in a weird
    place."""
    return home_dir() / "dep_snapshots"


def ensure_layout() -> None:
    """Create the standard ~/.deeptrade subtree if missing. Idempotent."""
    for d in (
        home_dir(),
        logs_dir(),
        reports_dir(),
        plugins_dir(),
        plugins_cache_dir(),
        dep_snapshots_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
