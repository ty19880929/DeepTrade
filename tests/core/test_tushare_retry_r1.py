"""HARD CONSTRAINT R1 — every retry attempt must re-enter `_TokenBucket.acquire()`.

If a future refactor moves bucket.acquire() out of `_do_fetch`, inlines the
retry loop, or otherwise lets retries skip the rate limiter, the tests below
will fail. That is by design.

Also covers end-to-end retry & cache-fallback integration for the new
`TushareTransportError`.

Background: see `tushare_transport_resilience_plan.md` §5 / §7.4.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.tushare_client import (
    FixtureTransport,
    TushareClient,
    TushareRateLimitError,
    TushareTransportError,
)

TEST_PLUGIN_ID = "test-plugin"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.duckdb")
    apply_core_migrations(d)
    return d


# ---------------------------------------------------------------------------
# Test transport that emits a fixed sequence of results / exceptions.
# Lets us inject "fail N times then succeed" without per-test boilerplate.
# ---------------------------------------------------------------------------


class SequencedTransport(FixtureTransport):
    """Pop one entry per call; raise if it's an Exception, return if a DataFrame."""

    def __init__(self, sequence: list[BaseException | pd.DataFrame]) -> None:
        super().__init__()
        self._sequence: list[BaseException | pd.DataFrame] = list(sequence)

    def call(self, api_name, params, fields):  # type: ignore[override]
        self.calls.append((api_name, dict(params)))
        if not self._sequence:
            raise AssertionError("SequencedTransport ran out of programmed responses")
        item = self._sequence.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item.copy()


# ===========================================================================
# R1 — token bucket runs on every attempt, including retries.
# ===========================================================================


def test_r1_bucket_acquire_runs_on_every_retry_attempt(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HARD CONSTRAINT R1.

    Three transient failures + one success → four attempts total → four
    `bucket.acquire()` calls.  Tenacity backoff is between attempts, but
    the bucket call sits inside `_do_fetch` so it runs anew for each.

    Failure mode this test catches: someone moves `bucket.acquire()` out
    of `_do_fetch` (e.g. into `__init__` or `call()`), or replaces the
    `Retrying` wrapper with a manual loop that forgets to re-enter
    `_do_fetch`. Either change drops the assertion below to 1.
    """
    transport = SequencedTransport(
        [
            TushareTransportError("attempt 1 — premature close"),
            TushareTransportError("attempt 2 — connection reset"),
            TushareTransportError("attempt 3 — read timeout"),
            pd.DataFrame({"ts_code": ["X"], "trade_date": ["20260509"]}),
        ]
    )
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, max_retries=5)
    # Skip backoff sleeps; we measure invocation count not wall time.
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)

    # Spy on bucket.acquire — count entries.
    acquire_calls: list[None] = []
    real_acquire = cli._bucket.acquire

    def spy_acquire() -> None:
        acquire_calls.append(None)
        real_acquire()

    monkeypatch.setattr(cli._bucket, "acquire", spy_acquire)

    df = cli.call("daily", trade_date="20260509")
    assert len(df) == 1
    assert len(acquire_calls) == 4, (
        f"R1 violated: bucket.acquire ran {len(acquire_calls)} times for 4 attempts. "
        "Every retry must re-enter _do_fetch and pass through the rate limiter."
    )
    assert len(transport.calls) == 4


def test_r1_bucket_acquire_runs_for_429_retries_too(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 also holds for rate-limit retries — the decayed bucket must still
    gate every attempt, never bypassed by the retry path."""
    transport = SequencedTransport(
        [
            TushareRateLimitError("429 #1"),
            TushareRateLimitError("429 #2"),
            pd.DataFrame({"ts_code": ["X"], "trade_date": ["20260509"]}),
        ]
    )
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=100.0, max_retries=5)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)

    initial_rps = cli.rps
    acquire_calls: list[float] = []
    real_acquire = cli._bucket.acquire

    def spy_acquire() -> None:
        acquire_calls.append(cli.rps)  # capture rps AT THE MOMENT acquire is called
        real_acquire()

    monkeypatch.setattr(cli._bucket, "acquire", spy_acquire)

    df = cli.call("daily", trade_date="20260509")
    assert len(df) == 1
    assert len(acquire_calls) == 3, "R1: 2 failures + 1 success = 3 bucket.acquire calls"
    # First attempt sees full rps; later attempts see decayed rps (decay 0.5 per 429).
    assert acquire_calls[0] == initial_rps
    assert acquire_calls[1] < acquire_calls[0]
    assert acquire_calls[2] < acquire_calls[1]


# ===========================================================================
# Integration — TushareTransportError flows through retry + cache fallback.
# ===========================================================================


def test_transport_error_is_retried_then_succeeds(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`TushareTransportError` is (by inheritance) `TushareServerError`, so
    the existing `retry_if_exception_type` whitelist picks it up.
    """
    transport = SequencedTransport(
        [
            TushareTransportError("Response ended prematurely"),
            TushareTransportError("Connection reset by peer"),
            pd.DataFrame({"ts_code": ["X"], "trade_date": ["20260509"]}),
        ]
    )
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)

    df = cli.call("daily", trade_date="20260509")
    assert len(df) == 1
    assert len(transport.calls) == 3


def test_transport_error_falls_back_to_cache_after_retries_exhausted(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent transport failure on a date that already has a cached payload
    must serve the cached payload via the existing 5xx-fallback path, NOT
    propagate the error and terminate training.
    """
    # First call: succeed and seed the cache.
    seed_df = pd.DataFrame({"ts_code": ["X", "Y"], "trade_date": ["20260509"] * 2})

    transport = SequencedTransport(
        [
            seed_df,
            # Then exhaust the retry budget with transport errors on a force_sync.
            *(TushareTransportError("Response ended prematurely") for _ in range(20)),
        ]
    )
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, max_retries=5)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)

    cli.call("limit_list_d", trade_date="20260509")  # seeds cache

    # force_sync re-fetches; transport keeps failing; fallback returns cached payload.
    df = cli.call("limit_list_d", trade_date="20260509", force_sync=True)
    assert len(df) == 2  # served from cache, not raised


def test_transport_error_propagates_when_no_cache_available(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cache + retries exhausted → still raises (so caller can decide what
    to do). The error is the actual transport error, not a generic TushareError.
    """
    transport = SequencedTransport(
        [TushareTransportError("Response ended prematurely") for _ in range(20)]
    )
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, max_retries=3)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)

    with pytest.raises(TushareTransportError):
        cli.call("daily", trade_date="20260509")
    assert len(transport.calls) == 3  # honored max_retries


# ===========================================================================
# `max_retries` is wired through the constructor.
# ===========================================================================


def test_max_retries_constructor_is_honored(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SequencedTransport([TushareTransportError("err") for _ in range(20)])
    cli = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, max_retries=2)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)
    with pytest.raises(TushareTransportError):
        cli.call("daily", trade_date="20260509")
    assert len(transport.calls) == 2


def test_max_retries_default_is_seven(db: Database) -> None:
    """Default value must stay at 7 — documented in the design doc and
    relied upon for the worst-case ~70s wait budget."""
    cli = TushareClient(db, FixtureTransport(), plugin_id=TEST_PLUGIN_ID, rps=1000.0)
    # tenacity's stop_after_attempt stores the limit on `max_attempt_number`.
    assert cli._retrying.stop.max_attempt_number == 7  # type: ignore[union-attr]
