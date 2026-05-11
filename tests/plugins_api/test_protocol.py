"""Plugin contract tests — minimal Plugin Protocol.

Asserts the public surface contract:
    * A class with metadata + validate_static + dispatch satisfies Plugin
    * A class missing dispatch does NOT satisfy Plugin
    * runtime_checkable isinstance() works for Plugin
"""

from __future__ import annotations

from deeptrade.plugins_api import Plugin


class GoodPlugin:
    metadata = None

    def validate_static(self, ctx): ...  # noqa: ANN001, ARG002

    def dispatch(self, argv):  # noqa: ANN001
        return 0


class IncompletePlugin:
    """Missing dispatch — should NOT satisfy Plugin."""

    metadata = None

    def validate_static(self, ctx): ...  # noqa: ANN001, ARG002


def test_complete_class_is_a_plugin() -> None:
    assert isinstance(GoodPlugin(), Plugin)


def test_class_missing_dispatch_is_not_a_plugin() -> None:
    # Protocol.runtime_checkable considers ALL declared members.
    # IncompletePlugin lacks dispatch → not an isinstance.
    assert not isinstance(IncompletePlugin(), Plugin)
