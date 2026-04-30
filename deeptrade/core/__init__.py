"""Core services layer.

Houses cross-cutting infrastructure (DB, config, secrets, clients, notifier)
that plugins consume directly via the public ``deeptrade.plugins_api`` surface
and the top-level ``deeptrade.notify`` / ``deeptrade.notification_session`` API.
"""

from __future__ import annotations
