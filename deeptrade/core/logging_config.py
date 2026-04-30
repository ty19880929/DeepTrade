"""Logging configuration with file rotation.

ADR-006: log records go to STDERR (avoiding stdout where questionary lives) and
to a rotating file under ~/.deeptrade/logs/.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from deeptrade.core import paths

DEFAULT_LOG_FILENAME = "deeptrade.log"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
DEFAULT_BACKUP_COUNT = 5


def setup_logging(
    *,
    level: str = "INFO",
    log_filename: str = DEFAULT_LOG_FILENAME,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> None:
    """Configure root logger with stderr + rotating file handler.

    Idempotent — calling twice replaces existing deeptrade handlers.
    """
    root = logging.getLogger()
    # Remove any prior deeptrade-tagged handlers so configure-after-init works
    for h in list(root.handlers):
        if getattr(h, "_deeptrade", False):
            root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # stderr (so stdout stays clean for questionary / dashboards)
    stderr_h = logging.StreamHandler(stream=sys.stderr)
    stderr_h.setFormatter(fmt)
    stderr_h._deeptrade = True  # type: ignore[attr-defined]
    root.addHandler(stderr_h)

    # rotating file under ~/.deeptrade/logs/
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    file_h = RotatingFileHandler(
        log_dir / log_filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    file_h._deeptrade = True  # type: ignore[attr-defined]
    root.addHandler(file_h)

    root.setLevel(level)
