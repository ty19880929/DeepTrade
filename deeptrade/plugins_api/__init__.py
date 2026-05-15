"""Public plugin API.

All plugins import from this package; the rest of ``deeptrade.*`` is internal.

Stable surface:

* Contract:        Plugin (Protocol), PluginContext (api_version "1" and "2").
* Metadata schema: PluginMetadata, TableSpec, MigrationSpec, PluginPermissions,
                   TushareApiPermissions.
* LLM profile:     StageProfile — plugin-owned preset → stage mapping.
* Services:        LLMManager, TushareClient — construct directly from
                   :class:`PluginContext` primitives. v0.6+ promotes these
                   to the public surface (was: ``deeptrade.core.*`` internals).
                   Lazy-loaded via PEP 562 ``__getattr__`` so ``TushareClient``
                   never drags ``pandas`` onto framework startup (the
                   ``plugin-runtime`` extras separation depends on this).
* Errors:          render_exception — DEEPTRADE_DEBUG-aware formatter for
                   dispatch tails; debug_enabled — env-flag probe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deeptrade.plugins_api.base import Plugin, PluginContext
from deeptrade.plugins_api.errors import debug_enabled, render_exception
from deeptrade.plugins_api.llm import StageProfile
from deeptrade.plugins_api.metadata import (
    MigrationSpec,
    PluginMetadata,
    PluginPermissions,
    TableSpec,
    TushareApiPermissions,
)

if TYPE_CHECKING:  # pragma: no cover — import-time hint only
    from deeptrade.core.llm_manager import LLMManager
    from deeptrade.core.tushare_client import TushareClient

__all__ = [
    "LLMManager",
    "MigrationSpec",
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "PluginPermissions",
    "StageProfile",
    "TableSpec",
    "TushareApiPermissions",
    "TushareClient",
    "debug_enabled",
    "render_exception",
]


# PEP 562 lazy attribute resolution.
#
# Eagerly importing ``deeptrade.core.tushare_client`` here would pull
# ``pandas`` into every framework startup — but ``pandas`` is intentionally
# NOT a framework dependency (see ``pyproject.toml::optional-dependencies
# .plugin-runtime``). Wheels published to PyPI install with only the 11
# framework deps; plugins that need TushareClient install pandas via their
# own ``deeptrade_plugin.yaml::dependencies``.
#
# Resolving ``LLMManager`` / ``TushareClient`` on first attribute access
# keeps the public surface promise (you can ``from deeptrade.plugins_api
# import TushareClient``) while preserving the import-time invariant.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LLMManager": ("deeptrade.core.llm_manager", "LLMManager"),
    "TushareClient": ("deeptrade.core.tushare_client", "TushareClient"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415

    module = importlib.import_module(target[0])
    return getattr(module, target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
