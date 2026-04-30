"""DeepTrade — LLM-driven A-share stock screening CLI."""

from __future__ import annotations

from deeptrade.core.notifier import notification_session, notify

__version__ = "0.0.1"
__all__ = ["__version__", "notification_session", "notify"]
