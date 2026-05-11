"""V0.3 DoD — TushareClient: cache classes, intraday isolation, fallback, retries.

14 keypoints (PLAN.md §4 V0.3 DoD list).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.tushare_client import (
    DEFAULT_HOT_TTL_SECONDS,
    STATIC_TTL_SECONDS,
    FixtureTransport,
    SyncState,
    TushareClient,
    TushareRateLimitError,
    TushareServerError,
    TushareUnauthorizedError,
    can_fallback,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.duckdb")
    apply_core_migrations(d)
    return d


@pytest.fixture
def transport() -> FixtureTransport:
    return FixtureTransport()


TEST_PLUGIN_ID = "test-plugin"


@pytest.fixture
def client(db: Database, transport: FixtureTransport) -> TushareClient:
    return TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0)


# ---------------------------------------------------------------------------
# DoD 1 — uses fixture transport
# ---------------------------------------------------------------------------


def test_call_uses_fixture_transport_when_injected(
    client: TushareClient, transport: FixtureTransport
) -> None:
    transport.register("stock_basic", pd.DataFrame({"ts_code": ["000001.SZ"]}))
    df = client.call("stock_basic")
    assert len(df) == 1
    assert transport.calls == [("stock_basic", {})]


# ---------------------------------------------------------------------------
# DoD 2 — cache hit skips API
# ---------------------------------------------------------------------------


def test_cache_hit_skips_api_call(client: TushareClient, transport: FixtureTransport) -> None:
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X"], "trade_date": ["20260427"]}))
    client.call("limit_list_d", trade_date="20260427")
    assert len(transport.calls) == 1

    # Second call should hit cache, NOT transport.
    client.call("limit_list_d", trade_date="20260427")
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# DoD 3 — static cache_class respects 7-day TTL
# ---------------------------------------------------------------------------


def test_cache_class_static_respects_7day_ttl(db: Database, client: TushareClient) -> None:
    # Manually insert a state row that's 8 days old → should miss
    expired_at = datetime.now() - timedelta(seconds=STATIC_TTL_SECONDS + 60)
    db.execute(
        "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status, row_count, "
        "cache_class, data_completeness, synced_at) "
        "VALUES (?, 'stock_basic', '*', 'ok', 5, 'static', 'final', ?)",
        (TEST_PLUGIN_ID, expired_at),
    )
    state = client._read_state("stock_basic", "*")  # type: ignore[attr-defined]
    assert state is not None
    # Internal predicate: cache should NOT hit because TTL expired
    assert not client._cache_hit(state, "static")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DoD 4 — trade_day_immutable: never refetch after status=ok
# ---------------------------------------------------------------------------


def test_cache_class_immutable_no_refetch_after_ok(
    client: TushareClient, transport: FixtureTransport
) -> None:
    transport.register("limit_step", pd.DataFrame({"nums": ["3"]}))
    client.call("limit_step", trade_date="20260427")
    # Second call after a hypothetical 100-day sleep would still hit cache
    state = client._read_state("limit_step", "20260427")  # type: ignore[attr-defined]
    state.synced_at = datetime.now() - timedelta(days=100)  # type: ignore[union-attr]
    assert client._cache_hit(state, "trade_day_immutable")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DoD 5 — trade_day_mutable: refetch on T or T+1
# ---------------------------------------------------------------------------


def test_cache_class_mutable_refetches_on_T_or_T_plus_1(
    db: Database, client: TushareClient, transport: FixtureTransport
) -> None:
    today = datetime.now().date().strftime("%Y%m%d")
    transport.register("moneyflow", pd.DataFrame({"ts_code": ["A"]}))
    client.call("moneyflow", trade_date=today)
    state = client._read_state("moneyflow", today)  # type: ignore[attr-defined]
    assert state is not None
    # mutable + (T or T+1) → cache miss
    assert not client._cache_hit(state, "trade_day_mutable")  # type: ignore[attr-defined]

    # But for an old trade_date (>1 day), mutable is allowed
    old = (datetime.now().date() - timedelta(days=10)).strftime("%Y%m%d")
    db.execute(
        "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status, row_count, "
        "cache_class, data_completeness, synced_at) "
        "VALUES (?, 'moneyflow', ?, 'ok', 5, 'trade_day_mutable', 'final', CURRENT_TIMESTAMP)",
        (TEST_PLUGIN_ID, old),
    )
    state2 = client._read_state("moneyflow", old)  # type: ignore[attr-defined]
    assert client._cache_hit(state2, "trade_day_mutable")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DoD 6 — hot_or_anns: configurable TTL
# ---------------------------------------------------------------------------


def test_cache_class_hot_or_anns_respects_6h_ttl(db: Database, client: TushareClient) -> None:
    expired_at = datetime.now() - timedelta(seconds=DEFAULT_HOT_TTL_SECONDS + 1)
    db.execute(
        "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status, row_count, "
        "cache_class, ttl_seconds, data_completeness, synced_at) "
        "VALUES (?, 'ths_hot', '20260427', 'ok', 10, 'hot_or_anns', ?, 'final', ?)",
        (TEST_PLUGIN_ID, DEFAULT_HOT_TTL_SECONDS, expired_at),
    )
    state = client._read_state("ths_hot", "20260427")  # type: ignore[attr-defined]
    assert not client._cache_hit(state, "hot_or_anns")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DoD 7 — F4 fix: intraday run writes data_completeness='intraday'
# ---------------------------------------------------------------------------


def test_intraday_run_writes_data_completeness_intraday(
    db: Database, transport: FixtureTransport
) -> None:
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X"]}))
    client = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=True)
    client.call("limit_list_d", trade_date="20260427")

    row = db.fetchone(
        "SELECT data_completeness FROM tushare_sync_state "
        "WHERE api_name='limit_list_d' AND trade_date='20260427'"
    )
    assert row is not None and row[0] == "intraday"


# ---------------------------------------------------------------------------
# DoD 8 — F4 fix: daily-mode rejects intraday cache and refetches
# ---------------------------------------------------------------------------


def test_eod_run_rejects_intraday_cache_and_refetches(
    db: Database, transport: FixtureTransport
) -> None:
    # Step 1: intraday run writes data_completeness='intraday'
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X"]}))
    intraday_client = TushareClient(
        db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=True
    )
    intraday_client.call("limit_list_d", trade_date="20260427")
    assert len(transport.calls) == 1

    # Step 2: daily-mode (intraday=False) reader must refetch
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X", "Y"]}))
    eod_client = TushareClient(db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=False)
    df = eod_client.call("limit_list_d", trade_date="20260427")
    assert len(transport.calls) == 2  # NEW transport call happened
    assert len(df) == 2  # got the refreshed payload

    # Now state has data_completeness='final'
    row = db.fetchone(
        "SELECT data_completeness FROM tushare_sync_state "
        "WHERE api_name='limit_list_d' AND trade_date='20260427'"
    )
    assert row is not None and row[0] == "final"


# ---------------------------------------------------------------------------
# DoD 9 — intraday run can use intraday cache
# ---------------------------------------------------------------------------


def test_intraday_run_can_use_intraday_cache(db: Database, transport: FixtureTransport) -> None:
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X"]}))
    intraday_client = TushareClient(
        db, transport, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=True
    )
    intraday_client.call("limit_list_d", trade_date="20260427")
    intraday_client.call("limit_list_d", trade_date="20260427")
    assert len(transport.calls) == 1  # second call hit cache


# ---------------------------------------------------------------------------
# DoD 10 — unauthorized marks state, raises typed error
# ---------------------------------------------------------------------------


def test_unauthorized_marks_state_and_raises(
    db: Database, transport: FixtureTransport, client: TushareClient
) -> None:
    transport.register("anns_d", TushareUnauthorizedError("no permission"))
    with pytest.raises(TushareUnauthorizedError):
        client.call("anns_d", trade_date="20260427")
    row = db.fetchone(
        "SELECT status FROM tushare_sync_state WHERE api_name='anns_d' AND trade_date='20260427'"
    )
    assert row is not None and row[0] == "unauthorized"


# ---------------------------------------------------------------------------
# DoD 11 — 429 triggers rps decay
# ---------------------------------------------------------------------------


def test_429_triggers_rps_decay(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    # Always rate-limit
    class AlwaysLimited(FixtureTransport):
        def call(self, api_name, params, fields):  # type: ignore[override]
            self.calls.append((api_name, dict(params)))
            raise TushareRateLimitError("rate limited")

    t = AlwaysLimited()
    cli = TushareClient(db, t, plugin_id=TEST_PLUGIN_ID, rps=10.0)
    # Skip tenacity backoff sleeps for fast test execution. The Retrying
    # instance now lives on the client (per-instance) instead of the function
    # decorator, so patch the instance attribute.
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)
    initial = cli.rps
    with pytest.raises(TushareRateLimitError):
        cli.call("daily", trade_date="20260427")
    assert cli.rps < initial  # tenacity retried, each 429 decays


# ---------------------------------------------------------------------------
# DoD 12 — 5xx retries then succeeds
# ---------------------------------------------------------------------------


def test_5xx_retries_then_succeeds(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    class FlakyTransport(FixtureTransport):
        def __init__(self) -> None:
            super().__init__()
            self._fail_left = 2

        def call(self, api_name, params, fields):  # type: ignore[override]
            self.calls.append((api_name, dict(params)))
            if self._fail_left > 0:
                self._fail_left -= 1
                raise TushareServerError("502 bad gateway")
            return pd.DataFrame({"ts_code": ["X"]})

    t = FlakyTransport()
    cli = TushareClient(db, t, plugin_id=TEST_PLUGIN_ID, rps=1000.0)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)
    df = cli.call("daily", trade_date="20260427")
    assert len(df) == 1
    assert len(t.calls) == 3  # 2 failures + 1 success


# ---------------------------------------------------------------------------
# DoD 13 — can_fallback rejects intraday cache for daily-mode runs (F4)
# ---------------------------------------------------------------------------


def test_can_fallback_rejects_intraday_for_eod_run() -> None:
    state = SyncState(
        plugin_id=TEST_PLUGIN_ID,
        api_name="limit_list_d",
        trade_date="20260427",
        status="ok",
        row_count=80,
        cache_class="trade_day_immutable",
        ttl_seconds=None,
        data_completeness="intraday",
        synced_at=datetime.now(),
    )
    assert can_fallback(state, "20260427", is_intraday_run=False) is False
    assert can_fallback(state, "20260427", is_intraday_run=True) is True


# ---------------------------------------------------------------------------
# DoD 14 — can_fallback accepts row_count=0 when status=ok (S4)
# ---------------------------------------------------------------------------


def test_can_fallback_accepts_row_count_zero_when_status_ok() -> None:
    state = SyncState(
        plugin_id=TEST_PLUGIN_ID,
        api_name="limit_list_d",
        trade_date="20260427",
        status="ok",
        row_count=0,  # extreme market: zero limit-up stocks is a LEGAL outcome
        cache_class="trade_day_immutable",
        ttl_seconds=None,
        data_completeness="final",
        synced_at=datetime.now(),
    )
    assert can_fallback(state, "20260427", is_intraday_run=False) is True


def test_can_fallback_rejects_when_status_not_ok() -> None:
    state = SyncState(
        plugin_id=TEST_PLUGIN_ID,
        api_name="limit_list_d",
        trade_date="20260427",
        status="failed",
        row_count=None,
        cache_class="trade_day_immutable",
        ttl_seconds=None,
        data_completeness="final",
        synced_at=datetime.now(),
    )
    assert can_fallback(state, "20260427", is_intraday_run=False) is False


def test_can_fallback_rejects_when_trade_date_mismatch() -> None:
    state = SyncState(
        plugin_id=TEST_PLUGIN_ID,
        api_name="limit_list_d",
        trade_date="20260426",  # different date
        status="ok",
        row_count=80,
        cache_class="trade_day_immutable",
        ttl_seconds=None,
        data_completeness="final",
        synced_at=datetime.now(),
    )
    assert can_fallback(state, "20260427", is_intraday_run=False) is False


# ---------------------------------------------------------------------------
# Misc: tushare_calls audit log writes
# ---------------------------------------------------------------------------


def test_tushare_calls_audit_log(
    db: Database, transport: FixtureTransport, client: TushareClient
) -> None:
    transport.register("stock_basic", pd.DataFrame({"ts_code": ["X"]}))
    client.call("stock_basic")
    rows = db.fetchall("SELECT api_name, rows FROM tushare_calls")
    assert rows and rows[0][0] == "stock_basic"
    assert rows[0][1] == 1


# ---------------------------------------------------------------------------
# B1.4 — 5xx fallback to cached payload
# ---------------------------------------------------------------------------


def test_5xx_falls_back_to_existing_final_cache(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call ok seeds cache; second call 5xx → fallback returns cached payload."""

    class FlakyAfterFirst(FixtureTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_done = False

        def call(self, api_name, params, fields):  # type: ignore[override]
            self.calls.append((api_name, dict(params)))
            if not self.first_done:
                self.first_done = True
                return pd.DataFrame({"ts_code": ["X", "Y"], "trade_date": ["20260427"] * 2})
            raise TushareServerError("503 second attempt")

    t = FlakyAfterFirst()
    cli = TushareClient(db, t, plugin_id=TEST_PLUGIN_ID, rps=1000.0)
    monkeypatch.setattr(cli._retrying, "sleep", lambda *_: None)
    cli.call("limit_list_d", trade_date="20260427")  # seeds cache
    # Force re-fetch: the second call would normally hit cache; force_sync triggers network
    df = cli.call("limit_list_d", trade_date="20260427", force_sync=True)
    assert len(df) == 2  # fallback returned cached payload, not raised


def test_5xx_no_fallback_when_intraday_state_in_eod_run(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FlakyAfterFirst(FixtureTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_done = False

        def call(self, api_name, params, fields):  # type: ignore[override]
            self.calls.append((api_name, dict(params)))
            if not self.first_done:
                self.first_done = True
                return pd.DataFrame({"ts_code": ["X"]})
            raise TushareServerError("503")

    t = FlakyAfterFirst()
    intraday_cli = TushareClient(db, t, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=True)
    monkeypatch.setattr(intraday_cli._retrying, "sleep", lambda *_: None)
    intraday_cli.call("limit_list_d", trade_date="20260427")  # state.data_completeness='intraday'

    # Daily-mode read of same date with force_sync → fetch → 5xx → fallback rejected
    eod_cli = TushareClient(db, t, plugin_id=TEST_PLUGIN_ID, rps=1000.0, intraday=False)
    monkeypatch.setattr(eod_cli._retrying, "sleep", lambda *_: None)
    with pytest.raises(TushareServerError):
        eod_cli.call("limit_list_d", trade_date="20260427", force_sync=True)


# ---------------------------------------------------------------------------
# B2.4 — cache_hit requires payload presence (atomicity defense)
# ---------------------------------------------------------------------------


def test_cache_hit_requires_payload_present(
    db: Database, transport: FixtureTransport, client: TushareClient
) -> None:
    """If sync_state.status='ok' but tushare_cache_blob has no row → not a hit."""
    # Seed state but NOT payload
    db.execute(
        "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status, row_count, "
        "cache_class, data_completeness) "
        "VALUES (?, 'limit_list_d', '20260427', 'ok', 5, 'trade_day_immutable', 'final')",
        (TEST_PLUGIN_ID,),
    )
    # Cache table doesn't even exist yet
    transport.register("limit_list_d", pd.DataFrame({"ts_code": ["X"]}))
    client.call("limit_list_d", trade_date="20260427")
    # Should hit transport (because no payload despite state=ok)
    assert len(transport.calls) == 1
