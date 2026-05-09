"""DeepTrade — LLM-driven A-share stock screening CLI."""

from __future__ import annotations

from deeptrade.core.notifier import notification_session, notify

__version__ = "0.2.0"
__all__ = ["__version__", "notification_session", "notify"]
