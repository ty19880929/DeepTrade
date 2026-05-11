"""Core services layer.

Houses cross-cutting infrastructure (DB, config, secrets, clients) that plugins
consume directly via the public ``deeptrade.plugins_api`` surface.
"""

from __future__ import annotations
