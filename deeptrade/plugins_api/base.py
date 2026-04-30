"""Plugin contract — api_version "1".

A plugin is a Python class implementing this Protocol. The framework loads it
via the dotted entrypoint declared in the YAML metadata. The framework knows
NOTHING about the plugin's domain semantics; the plugin owns its own command
parsing, execution, persistence, and output.

Channel plugins additionally implement :class:`deeptrade.plugins_api.channel.ChannelPlugin`
so the notifier can hand them payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.plugins_api.channel import PluginContext
    from deeptrade.plugins_api.metadata import PluginMetadata


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

    def dispatch(self, argv: list[str]) -> int:
        """CLI dispatch entry point.

        ``argv`` is the remaining command-line tail after the framework strips
        the leading ``<plugin_id>`` token. For example, when the user runs
        ``deeptrade limit-up-board run --force-sync``, the plugin receives
        ``argv == ['run', '--force-sync']``.

        The plugin owns parsing, ``--help`` rendering, execution, persistence,
        and output. Returns the process exit code (0 = success).
        """
