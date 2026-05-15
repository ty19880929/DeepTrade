"""Plugin contract — api_version "1" (legacy) and "2" (v0.6+).

A plugin is a Python class implementing this Protocol. The framework loads it
via the dotted entrypoint declared in the YAML metadata. The framework knows
NOTHING about the plugin's domain semantics; the plugin owns its own command
parsing, execution, persistence, and output.

``PluginContext`` is the minimal services bundle the framework hands to every
plugin's ``validate_static`` (during install). v0.6 ``api_version="2"``
plugins ALSO receive it at dispatch time, removing the need to reach into
``deeptrade.core.*`` from plugin code.

Dispatch signatures by ``api_version``:

* ``"1"`` — ``def dispatch(self, argv: list[str]) -> int`` (legacy).
  Plugin reaches back into ``deeptrade.core.*`` for DB / config / etc.
* ``"2"`` — ``def dispatch(self, ctx: PluginContext, argv: list[str]) -> int``.
  Framework hands the same ``PluginContext`` shape ``validate_static``
  gets, so plugins can stay on the public surface.

Both versions remain supported; v0.6 does NOT deprecate v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.config import ConfigService
    from deeptrade.core.db import Database
    from deeptrade.plugins_api.metadata import PluginMetadata


@dataclass
class PluginContext:
    """Minimal services bundle the framework hands to a plugin's
    ``validate_static`` (during install).

    Plugins that need richer services (TushareClient, LLMManager / LLMClient, etc.)
    construct them inside their own ``dispatch`` from these primitives.
    """

    db: Database
    config: ConfigService
    plugin_id: str | None = None


@runtime_checkable
class Plugin(Protocol):
    """The minimal interface a plugin entrypoint class must satisfy."""

    metadata: PluginMetadata

    def validate_static(self, ctx: PluginContext) -> None:
        """Post-install self-check. MUST NOT touch the network.

        Called once by the framework during ``deeptrade plugin install``. Use
        it to verify required tables exist, required config keys are present,
        and the entrypoint class loads cleanly. Raise on failure to abort
        install (the framework will roll back).
        """

    def dispatch(self, *args: object) -> int:
        """CLI dispatch entry point.

        Signature depends on ``metadata.api_version``:

        * ``"1"`` — ``dispatch(self, argv: list[str]) -> int``.
        * ``"2"`` — ``dispatch(self, ctx: PluginContext, argv: list[str]) -> int``.

        ``argv`` is the remaining command-line tail after the framework strips
        the leading ``<plugin_id>`` token. For example, when the user runs
        ``deeptrade limit-up-board run --force-sync``, the plugin receives
        ``argv == ['run', '--force-sync']``.

        The plugin owns parsing, ``--help`` rendering, execution, persistence,
        and output. Returns the process exit code (0 = success).

        The Protocol is declared variadic so ``isinstance(obj, Plugin)`` works
        for both v1 and v2 implementations — ``runtime_checkable`` only
        verifies attribute presence, never argument arity. The framework's
        ``cli._dispatch`` decides which form to call based on
        ``metadata.api_version``.
        """
