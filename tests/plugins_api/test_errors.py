"""Tests for ``deeptrade.plugins_api.errors`` — DEEPTRADE_DEBUG-aware
exception rendering for plugin dispatch tails."""

from __future__ import annotations

import pytest

from deeptrade.plugins_api import debug_enabled, render_exception


def _make_chained_error() -> Exception:
    """Build an exception with both ``__traceback__`` and ``__cause__`` set,
    matching what a real `try/except ... raise X from e` would yield."""
    try:
        try:
            raise ValueError("inner cause")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as e:
        return e


# --- default mode (no DEBUG) ---------------------------------------------


def test_render_exception_one_liner_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPTRADE_DEBUG", raising=False)
    try:
        raise TypeError("list indices must be integers or slices, not str")
    except TypeError as e:
        out = render_exception(e)
    assert out == "✘ TypeError: list indices must be integers or slices, not str"
    assert "\n" not in out, "default mode must be a single line"


def test_render_exception_custom_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPTRADE_DEBUG", raising=False)
    e = ValueError("bad input")
    out = render_exception(e, header_glyph="!!")
    assert out == "!! ValueError: bad input"


# --- DEBUG mode ----------------------------------------------------------


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "On"])
def test_render_exception_traceback_when_debug(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("DEEPTRADE_DEBUG", truthy)
    e = _make_chained_error()
    out = render_exception(e)
    # Header glyph still present
    assert out.startswith("✘ ")
    # Outer exception + message present
    assert "RuntimeError: outer failure" in out
    # Chained cause surfaced (this is the user-facing payoff over the one-liner)
    assert "ValueError: inner cause" in out
    # Traceback frames present (file + lineno)
    assert "Traceback" in out
    # No trailing newline — caller appends its own
    assert not out.endswith("\n")


def test_render_exception_falsy_env_still_one_liner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPTRADE_DEBUG", "0")
    e = RuntimeError("x")
    assert render_exception(e) == "✘ RuntimeError: x"


def test_debug_enabled_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPTRADE_DEBUG", raising=False)
    assert debug_enabled() is False
    monkeypatch.setenv("DEEPTRADE_DEBUG", "1")
    assert debug_enabled() is True
    monkeypatch.setenv("DEEPTRADE_DEBUG", "0")
    assert debug_enabled() is False
