"""Plugin contract tests — minimal Plugin Protocol + ChannelPlugin extension.

Asserts the public surface contract:
    * A class with metadata + validate_static + dispatch satisfies Plugin
    * A class missing dispatch does NOT satisfy Plugin
    * ChannelPlugin requires push() in addition to the Plugin shape
    * runtime_checkable isinstance() works for both Protocols
"""

from __future__ import annotations

from deeptrade.plugins_api import ChannelPlugin, Plugin


class GoodPlugin:
    metadata = None

    def validate_static(self, ctx): ...  # noqa: ANN001, ARG002

    def dispatch(self, argv):  # noqa: ANN001
        return 0


class IncompletePlugin:
    """Missing dispatch — should NOT satisfy Plugin."""

    metadata = None

    def validate_static(self, ctx): ...  # noqa: ANN001, ARG002


class GoodChannel(GoodPlugin):
    def push(self, ctx, payload):  # noqa: ANN001, ARG002
        return


def test_complete_class_is_a_plugin() -> None:
    assert isinstance(GoodPlugin(), Plugin)


def test_class_missing_dispatch_is_not_a_plugin() -> None:
    # Protocol.runtime_checkable considers ALL declared members.
    # IncompletePlugin lacks dispatch → not an isinstance.
    assert not isinstance(IncompletePlugin(), Plugin)


def test_channel_plugin_requires_push_in_addition_to_dispatch() -> None:
    assert isinstance(GoodChannel(), ChannelPlugin)
    # A regular Plugin (no push) does NOT satisfy ChannelPlugin
    assert not isinstance(GoodPlugin(), ChannelPlugin)


def test_channel_plugin_is_also_a_plugin() -> None:
    """ChannelPlugin extends Plugin — every channel must satisfy both."""
    assert isinstance(GoodChannel(), Plugin)
