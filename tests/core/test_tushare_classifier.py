"""Tests for the Tushare exception classifier (`_classify_tushare_exception`).

Pinned guarantees:
    - Transport-layer transients across httpx/requests/urllib3/stdlib all
      land on `TushareTransportError` (a `TushareServerError` subclass), so
      they enter the existing retry + cache-fallback paths.
    - The original symptom — "Response ended prematurely" raised as a bare
      `Exception` — is in particular routed to `TushareTransportError`. This
      is the regression test for the bug that motivated this module.
    - HTTP status codes are extracted before falling back to string matching.
    - Tushare business-layer text (Chinese / English) for permission /
      rate-limit is still recognized via string matching.
    - Unknown errors default to `TushareTransportError` (retryable),
      not the historical `TushareError` (terminal). This is the key
      design inversion — keep it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deeptrade.core.tushare_client import (
    TushareError,
    TushareRateLimitError,
    TushareServerError,
    TushareTransportError,
    TushareUnauthorizedError,
    _classify_tushare_exception,
    _extract_http_status,
    _is_transient_transport_error,
)

# ---------------------------------------------------------------------------
# Synthetic exception types from each major HTTP stack.
#
# We don't import httpx / requests / urllib3 directly: doing so would force
# a hard test dependency on stacks the framework itself doesn't import.
# Type-name matching only inspects ``module.QualName``, so synthetic classes
# placed in modules with the right names suffice.
# ---------------------------------------------------------------------------


def _make_typed_exception(module: str, qualname: str, msg: str) -> Exception:
    """Forge an exception whose ``type(e).__module__`` and ``__qualname__``
    look like the real third-party class for classifier purposes.
    """
    cls = type(qualname, (Exception,), {})
    cls.__module__ = module
    cls.__qualname__ = qualname
    return cls(msg)


# ---------------------------------------------------------------------------
# Type-based classification — the primary signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "qualname", "msg"),
    [
        # httpx / h11 — the original "Response ended prematurely" path.
        ("httpx", "RemoteProtocolError", "Response ended prematurely"),
        ("h11", "RemoteProtocolError", "Server disconnected without sending a response"),
        # requests
        ("requests.exceptions", "ChunkedEncodingError", "Connection broken: 0 bytes read"),
        ("requests.exceptions", "ConnectionError", "HTTPSConnectionPool(...)"),
        ("requests.exceptions", "ReadTimeout", "Read timed out"),
        # urllib3
        ("urllib3.exceptions", "ProtocolError", "('Connection aborted.', ...)"),
        ("urllib3.exceptions", "IncompleteRead", "0 bytes read, 1024 more expected"),
        ("urllib3.exceptions", "ReadTimeoutError", "Read timed out"),
        # stdlib
        ("http.client", "RemoteDisconnected", "Remote end closed connection without response"),
    ],
)
def test_transport_layer_types_route_to_transport_error(
    module: str, qualname: str, msg: str
) -> None:
    """Every known transport-layer transient type → TushareTransportError."""
    e = _make_typed_exception(module, qualname, msg)
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError)
    # And — by inheritance — eligible for the existing retry whitelist.
    assert isinstance(out, TushareServerError)


def test_stdlib_connection_reset_routes_to_transport_error() -> None:
    """Built-in `ConnectionResetError` is in the type whitelist."""
    e = ConnectionResetError("[WinError 10054] An existing connection was forcibly closed")
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError)


def test_socket_timeout_routes_to_transport_error() -> None:
    """`socket.timeout` is an alias for `TimeoutError` in modern Python."""
    e = TimeoutError("Read timed out")
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError)


# ---------------------------------------------------------------------------
# REGRESSION: the original symptom must always retry.
# ---------------------------------------------------------------------------


def test_response_ended_prematurely_bare_exception_routes_to_transport_error() -> None:
    """REGRESSION: the historical bug.

    The Tushare SDK occasionally surfaces httpx's RemoteProtocolError as a
    plain `Exception("Response ended prematurely")`, hiding the original
    type. The classifier MUST still recognize the message string and route
    it to a retryable error. If this test fails, training jobs will start
    terminating on transient network jitter again.
    """
    e = Exception("Response ended prematurely")
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError), (
        "Response ended prematurely must always be retryable — see "
        "tushare_transport_resilience_plan.md"
    )


# ---------------------------------------------------------------------------
# HTTP status code extraction
# ---------------------------------------------------------------------------


def test_status_429_routes_to_rate_limit_error() -> None:
    e = Exception("client error")
    e.response = SimpleNamespace(status_code=429)  # type: ignore[attr-defined]
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareRateLimitError)


def test_status_5xx_routes_to_server_error() -> None:
    e = Exception("server error")
    e.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareServerError)
    # NOT a TushareTransportError specifically — server-side 5xx is its
    # own category (also retryable, but distinguishable in logs).
    assert not isinstance(out, TushareTransportError)


@pytest.mark.parametrize("status", [401, 403])
def test_status_4xx_auth_routes_to_unauthorized(status: int) -> None:
    e = Exception("denied")
    e.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareUnauthorizedError)


def test_status_extracted_from_leading_digits_in_message() -> None:
    """Some SDK wrappers prefix the code: '503: server busy'."""
    assert _extract_http_status(Exception("503: server busy")) == 503
    assert _extract_http_status(Exception("429 too many requests")) == 429


def test_status_extraction_returns_none_for_no_signal() -> None:
    assert _extract_http_status(Exception("totally weird message")) is None
    assert _extract_http_status(Exception("")) is None


# ---------------------------------------------------------------------------
# Tushare business-layer text matching (legacy path — must still work)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    ["权限不足", "未开通该接口权限", "permission denied", "no permission for this api"],
)
def test_permission_keywords_route_to_unauthorized(msg: str) -> None:
    out = _classify_tushare_exception(Exception(msg))
    assert isinstance(out, TushareUnauthorizedError)


@pytest.mark.parametrize(
    "msg",
    [
        "抱歉，您每分钟最多访问该接口500次",
        "频率超过限制",
        "限流中",
        "rate limit exceeded",
        "HTTP 429 ...",
    ],
)
def test_rate_limit_keywords_route_to_rate_limit(msg: str) -> None:
    out = _classify_tushare_exception(Exception(msg))
    assert isinstance(out, TushareRateLimitError)


# ---------------------------------------------------------------------------
# DEFAULT INVERSION — the central design change
# ---------------------------------------------------------------------------


def test_unknown_error_defaults_to_transport_error_not_terminal() -> None:
    """Pre-fix behavior: unknown → bare `TushareError` (no retry, training dies).
    Post-fix behavior: unknown → `TushareTransportError` (retried, may recover).

    If anyone "fixes" this test by swapping the assertion, please re-read
    section 5.1 of the design doc — the inversion is intentional.
    """
    e = Exception("weird new error nobody saw before")
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError)
    # Still part of the TushareError hierarchy → existing `except TushareError`
    # at call sites continues to work.
    assert isinstance(out, TushareError)


def test_unclassified_message_is_marked_as_such() -> None:
    """Unknown-default path tags the message so logs make the path obvious."""
    out = _classify_tushare_exception(Exception("nothing matches"))
    assert "unclassified" in str(out)


# ---------------------------------------------------------------------------
# Priority ordering: type-based wins over status code wins over keywords.
# Ensures we don't accidentally regress to the old "string-first" model.
# ---------------------------------------------------------------------------


def test_type_match_wins_over_misleading_message_keywords() -> None:
    """A RemoteProtocolError whose message happens to contain '权限'
    must still be classified as TransportError, not Unauthorized.
    Type wins.
    """
    e = _make_typed_exception(
        "httpx", "RemoteProtocolError", "权限 — but really the connection died"
    )
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareTransportError)


def test_status_code_wins_over_misleading_message_keywords() -> None:
    """A 429 with a message that mentions '权限' is rate-limit, not auth."""
    e = Exception("权限 issue")
    e.response = SimpleNamespace(status_code=429)  # type: ignore[attr-defined]
    out = _classify_tushare_exception(e)
    assert isinstance(out, TushareRateLimitError)


# ---------------------------------------------------------------------------
# `_is_transient_transport_error` direct tests (helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg_keyword",
    [
        "Response ended prematurely",
        "Remote protocol error",
        "Connection reset by peer",
        "Connection aborted",
        "Broken pipe",
        "Read timed out",
        "EOF occurred in violation of protocol",
        "Incomplete read",
    ],
)
def test_helper_recognizes_transient_message_keywords(msg_keyword: str) -> None:
    """Even a bare Exception with the right message wording counts."""
    e: Any = Exception(msg_keyword)
    assert _is_transient_transport_error(e, type(e).__module__ + "." + type(e).__qualname__)


def test_helper_rejects_clearly_non_transient_message() -> None:
    e = Exception("Invalid API token")
    assert not _is_transient_transport_error(e, "builtins.Exception")
