"""Error rendering helpers for plugin dispatch.

Plugins commonly install a generic ``except Exception`` at the top of their
``dispatch`` to translate any uncaught error into a non-zero exit code while
keeping the user-facing output short. The downside is the loss of the
traceback when something unexpected goes wrong.

``render_exception`` centralizes that one-liner/traceback decision behind a
single environment variable so users can opt into a full stack without
plugins inventing their own conventions:

    DEEPTRADE_DEBUG=1   → traceback (chained causes included)
    unset / "0" / ""    → one-line "{glyph} {ExcType}: {msg}"

Both the framework (``deeptrade/cli.py::_dispatch``) and plugin dispatch
tails should use this helper so the toggle is honored uniformly.
"""

from __future__ import annotations

import os
import traceback

_DEBUG_ENV = "DEEPTRADE_DEBUG"


def debug_enabled() -> bool:
    """True when ``DEEPTRADE_DEBUG`` is set to a truthy value."""
    return os.environ.get(_DEBUG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def render_exception(exc: BaseException, *, header_glyph: str = "✘") -> str:
    """Format ``exc`` for stderr.

    With ``DEEPTRADE_DEBUG=1`` returns the full ``traceback.format_exception``
    output (which already includes chained causes and exception groups)
    prefixed by ``header_glyph``. Otherwise returns the one-liner
    ``"{header_glyph} {ExcType}: {message}"``.

    The output has no trailing newline; callers append their own.
    """
    if debug_enabled():
        body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return f"{header_glyph} {body.rstrip()}"
    return f"{header_glyph} {type(exc).__name__}: {exc}"
