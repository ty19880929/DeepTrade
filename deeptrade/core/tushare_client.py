"""Tushare client with rate limiting, per-plugin caching, and intraday isolation.

Per-plugin scoping (data isolation model): every TushareClient instance is
bound to a single ``plugin_id``; ``tushare_sync_state``, ``tushare_calls``,
and ``tushare_cache_blob`` rows are all scoped by ``plugin_id``. Plugins do
NOT share cached payloads with each other — even if two plugins call the
same API for the same trade_date, they each maintain their own cache row.

The framework reserves the synthetic ``plugin_id == "__framework__"`` for
its own connectivity tests (``deeptrade config test``).

Cache class buckets:
    - static               : api_name × '*'           ; 7d TTL
    - trade_day_immutable  : api_name × trade_date    ; never refetch when ok
    - trade_day_mutable    : api_name × trade_date    ; allow T/T+1 refetch
    - hot_or_anns          : api_name × trade_date    ; configurable TTL
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import pandas as pd
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from deeptrade.core.db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache class taxonomy
# ---------------------------------------------------------------------------

CacheClass = Literal["static", "trade_day_immutable", "trade_day_mutable", "hot_or_anns"]

# Per-API cache class assignment (DESIGN §11.1 + §11.2 lists)
API_CACHE_CLASS: dict[str, CacheClass] = {
    # static
    "stock_basic": "static",
    "trade_cal": "static",
    # trade_day_immutable (settled at end of day, never revised)
    "daily": "trade_day_immutable",
    "limit_list_d": "trade_day_immutable",
    "limit_list_ths": "trade_day_immutable",
    "limit_step": "trade_day_immutable",
    "limit_cpt_list": "trade_day_immutable",
    "stock_st": "trade_day_immutable",
    "top_list": "trade_day_immutable",
    "top_inst": "trade_day_immutable",
    "stk_limit": "trade_day_immutable",
    "stk_auction_o": "trade_day_immutable",
    "suspend_d": "trade_day_immutable",
    "adj_factor": "trade_day_immutable",
    # trade_day_mutable (occasional T+1/T+2 corrections)
    "moneyflow": "trade_day_mutable",
    "moneyflow_ths": "trade_day_mutable",
    "daily_basic": "trade_day_mutable",
    # hot_or_anns (TTL-based)
    "ths_hot": "hot_or_anns",
    "dc_hot": "hot_or_anns",
    "anns_d": "hot_or_anns",
    "news": "hot_or_anns",
}

# Per-API "wide" fields list pushed to the transport so the on-disk cache
# always contains every column downstream code expects. Without this, tushare's
# default field subset for some APIs (notably stock_basic, which omits
# market/exchange/list_status) silently corrupts the cache and any READ-time
# field projection can never recover the missing columns.
# Add an entry whenever the default subset misses something the strategies need.
WIDE_FIELDS: dict[str, str] = {
    "stock_basic": (
        "ts_code,symbol,name,area,industry,fullname,enname,market,exchange,"
        "curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type"
    ),
}

# APIs whose intraday data is unstable; --allow-intraday triggers data_completeness='intraday'
INTRADAY_SENSITIVE_APIS: frozenset[str] = frozenset(
    {
        "limit_list_d",
        "limit_list_ths",
        "limit_step",
        "limit_cpt_list",
        "moneyflow",
        "moneyflow_ths",
        "daily",
        "daily_basic",
    }
)

# Default TTL for hot_or_anns class
DEFAULT_HOT_TTL_SECONDS = 6 * 3600

# Default TTL for static class
STATIC_TTL_SECONDS = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TushareError(Exception):
    """Base error from TushareClient."""


class TushareUnauthorizedError(TushareError):
    """Tushare reports the user lacks permission for this API."""


class TushareRateLimitError(TushareError):
    """HTTP 429 / equivalent — caller should slow down."""


class TushareServerError(TushareError):
    """5xx / transient transport error — eligible for retry."""


class TushareTransportError(TushareServerError):
    """Transport-layer transient failure — protocol error, connection reset,
    response ended prematurely, read timeout, etc.

    Subclass of ``TushareServerError`` so the existing retry whitelist
    (`retry_if_exception_type((TushareRateLimitError, TushareServerError))`)
    and the 5xx → cache fallback path (`_fetch_and_store`) both pick it up
    automatically. New callers don't need any code change.
    """


# ---------------------------------------------------------------------------
# Exception classifier — type-first, status-code-second, string-last,
# unknown-defaults-to-transient (so we retry rather than terminate).
# ---------------------------------------------------------------------------


_TRANSIENT_TYPE_KEYWORDS: tuple[str, ...] = (
    # httpx / h11
    "RemoteProtocolError",
    # http.client / requests / stdlib
    "RemoteDisconnected",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    # http.client / urllib3
    "IncompleteRead",
    # requests
    "ChunkedEncodingError",
    # urllib3
    "ProtocolError",
    # httpx / requests / urllib3
    "ReadTimeout",
    "ReadTimeoutError",
    "ConnectTimeout",
    "ConnectTimeoutError",
    # stdlib / asyncio
    "TimeoutError",
    # SSL handshake interruption — most are transient
    "SSLError",
)

_TRANSIENT_MSG_KEYWORDS: tuple[str, ...] = (
    # the original symptom that motivated this classifier
    "premature",
    "remote protocol",
    "remote disconnect",
    "incomplete read",
    "chunked",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "broken pipe",
    "timeout",
    "timed out",
    "eof occurred",
)


def _is_transient_transport_error(e: BaseException, type_name: str) -> bool:
    """True if ``e`` is a transport-layer transient failure across httpx /
    requests / urllib3 / stdlib.

    Type-name match is preferred (fully qualified ``module.QualName`` lets
    one keyword cover multiple stacks). Message-keyword fallback handles
    the case where the tushare SDK swallows the original exception type
    and re-raises a plain ``Exception(str)``.
    """
    if any(k in type_name for k in _TRANSIENT_TYPE_KEYWORDS):
        return True
    msg = str(e).lower()
    return any(k in msg for k in _TRANSIENT_MSG_KEYWORDS)


def _extract_http_status(e: BaseException) -> int | None:
    """Best-effort HTTP status extraction.

    Looks at ``e.response.status_code`` (httpx / requests) or the leading
    three digits of ``str(e)`` (some SDK wrappers prefix the code).
    """
    response = getattr(e, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        if isinstance(status, int):
            return status
    msg = str(e)
    if len(msg) >= 3 and msg[:3].isdigit():
        try:
            return int(msg[:3])
        except ValueError:
            return None
    return None


def _classify_tushare_exception(e: BaseException) -> TushareError:
    """Map an arbitrary upstream exception to the right TushareError subclass.

    Order matters:
        1. Exception type — the most reliable signal.
        2. HTTP status code if present on the exception.
        3. Tushare business-layer keywords (Chinese / English).
        4. Default to TushareTransportError (retryable). Inverting the
           default from "unknown → fatal" to "unknown → transient" is the
           central design change: a remote network service's unknown
           errors are far more likely transient than permanent. Worst case
           we waste a few retries; best case we ride out an outage that
           used to terminate hours of work.
    """
    type_name = f"{type(e).__module__}.{type(e).__qualname__}"

    # 1. Type-based — covers RemoteProtocolError / ChunkedEncodingError / ...
    if _is_transient_transport_error(e, type_name):
        return TushareTransportError(str(e))

    # 2. HTTP status code, if available
    status = _extract_http_status(e)
    if status is not None:
        if status == 429:
            return TushareRateLimitError(str(e))
        if 500 <= status < 600:
            return TushareServerError(str(e))
        if status in (401, 403):
            return TushareUnauthorizedError(str(e))

    # 3. Tushare business-layer text matching
    msg = str(e)
    low = msg.lower()
    if "权限" in msg or "未开通" in msg or "permission" in low or "no permission" in low:
        return TushareUnauthorizedError(msg)
    # Tushare's actual rate-limit response is the long-form
    # "抱歉，您每分钟最多访问该接口500次" — match the "每分钟" + "次" pair so
    # those, plus the shorter "频率"/"限流" variants, all funnel here.
    if (
        "频率" in msg
        or "限流" in msg
        or ("每分钟" in msg and "次" in msg)
        or "rate" in low
        or "429" in msg
    ):
        return TushareRateLimitError(msg)

    # 4. Default-inverted: unknown is treated as transient, not fatal.
    return TushareTransportError(f"unclassified: {msg}")


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------


class TushareTransport(ABC):
    """Abstract carrier for Tushare API calls. Production = SDK; tests = fixtures."""

    @abstractmethod
    def call(self, api_name: str, params: dict[str, Any], fields: str | None) -> pd.DataFrame:
        """Execute a single API call. Raise the typed error subclass on failure."""


class TushareSDKTransport(TushareTransport):
    """Production transport — wraps tushare.pro_api()."""

    def __init__(self, token: str) -> None:
        import tushare as ts  # noqa: PLC0415 — defer import to avoid hard dep at module load

        self._pro = ts.pro_api(token)

    def call(self, api_name: str, params: dict[str, Any], fields: str | None) -> pd.DataFrame:
        try:
            method = getattr(self._pro, api_name)
        except AttributeError as e:
            raise TushareError(f"unknown tushare api: {api_name}") from e

        kwargs = dict(params)
        if fields:
            kwargs["fields"] = fields
        try:
            df = method(**kwargs)
        except Exception as e:  # noqa: BLE001 — translate SDK errors uniformly
            raise _classify_tushare_exception(e) from e
        if df is None:
            return pd.DataFrame()
        return df


class FixtureTransport(TushareTransport):
    """Test transport — replays canned DataFrames keyed by (api, params)."""

    def __init__(self) -> None:
        self._fixtures: dict[str, pd.DataFrame | Exception] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []  # call audit log

    def register(
        self,
        api_name: str,
        result: pd.DataFrame | Exception,
        params: dict[str, Any] | None = None,
    ) -> None:
        key = self._key(api_name, params or {})
        self._fixtures[key] = result

    def call(self, api_name: str, params: dict[str, Any], fields: str | None) -> pd.DataFrame:
        self.calls.append((api_name, dict(params)))
        key = self._key(api_name, params)
        if key in self._fixtures:
            entry = self._fixtures[key]
        else:
            # fallback: any matching api_name (lets tests register without exact param match)
            for k, v in self._fixtures.items():
                if k.startswith(api_name + "|"):
                    entry = v
                    break
            else:
                raise TushareError(f"no fixture registered for {api_name} {params}")
        if isinstance(entry, Exception):
            raise entry
        return (
            entry.copy()
            if fields is None
            else entry[[c.strip() for c in fields.split(",") if c.strip() in entry.columns]].copy()
        )

    @staticmethod
    def _key(api_name: str, params: dict[str, Any]) -> str:
        body = json.dumps(params, sort_keys=True, default=str)
        return f"{api_name}|{body}"


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class _TokenBucket:
    def __init__(self, rps: float) -> None:
        self.rps = max(rps, 0.1)
        self._tokens = self.rps
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.rps, self._tokens + (now - self._last) * self.rps)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            need = (1.0 - self._tokens) / self.rps
        time.sleep(need)
        # reacquire
        with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)
            self._last = time.monotonic()

    def decay(self, factor: float = 0.5) -> None:
        with self._lock:
            self.rps = max(self.rps * factor, 0.1)


# ---------------------------------------------------------------------------
# Sync state record (mirrors tushare_sync_state row)
# ---------------------------------------------------------------------------


@dataclass
class SyncState:
    plugin_id: str
    api_name: str
    trade_date: str
    status: str  # ok | partial | failed | unauthorized
    row_count: int | None
    cache_class: CacheClass
    ttl_seconds: int | None
    data_completeness: str  # 'final' | 'intraday'
    synced_at: datetime


# Synthetic plugin_id for framework-level connectivity tests
# (deeptrade config test). Real plugin_ids cannot match this pattern (the
# Pydantic regex requires lowercase alnum + hyphen, no underscores).
FRAMEWORK_PLUGIN_ID: str = "__framework__"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TushareClient:
    """Cache-aware Tushare client. Bound to a single ``plugin_id``.

    Args:
        db: Open Database instance (for sync_state / calls / cached frames).
        transport: TushareTransport (real or fixture).
        plugin_id: scopes every cached row / audit row / sync-state row.
            Use ``FRAMEWORK_PLUGIN_ID`` for framework-level probes.
        rps: initial token-bucket rate (decays on 429).
        intraday: if True, all writes for INTRADAY_SENSITIVE_APIS get
                  data_completeness='intraday'; reads will only accept matching
                  completeness.
        max_retries: tenacity stop_after_attempt for transient errors
                     (rate limit + server + transport). Default 7 → worst-case
                     wait ≈ (1+2+4+8+16+30) ≈ 60s of jittered backoff.
        event_cb: optional callback for surfacing operationally-relevant
                  tushare events (5xx fallback, etc.) to the caller. Signature
                  ``event_cb(event_type, message, payload_dict)``. Kept as
                  plain strings to avoid plugins_api imports.
    """

    def __init__(
        self,
        db: Database,
        transport: TushareTransport,
        *,
        plugin_id: str,
        rps: float = 6.0,
        intraday: bool = False,
        max_retries: int = 7,
        event_cb: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._db = db
        self._transport = transport
        self._plugin_id = plugin_id
        self._bucket = _TokenBucket(rps)
        self._intraday = intraday
        self._event_cb = event_cb
        # R1 (HARD CONSTRAINT): the Retrying object wraps `_do_fetch`, whose
        # FIRST line is `self._bucket.acquire()`. Every retry attempt re-enters
        # `_do_fetch`, so the token bucket is honored on every attempt — the
        # tenacity backoff and the bucket throttle compose, never bypass.
        # Don't move bucket.acquire() out of `_do_fetch` without updating
        # `tests/core/test_tushare_retry_r1.py`.
        self._retrying = Retrying(
            retry=retry_if_exception_type((TushareRateLimitError, TushareServerError)),
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
            reraise=True,
        )

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def is_intraday(self) -> bool:
        return self._intraday

    @property
    def rps(self) -> float:
        return self._bucket.rps

    # --- B2.3 — materialize tushare frames into named business tables --------

    def materialize(
        self,
        table_name: str,
        df: pd.DataFrame,
        *,
        key_cols: list[str] | None = None,
    ) -> int:
        """Upsert ``df`` into the named DuckDB table (must already exist).

        Used by strategies to persist tushare returns into core shared tables
        (``stock_basic`` / ``daily`` / ``daily_basic``) and plugin tables
        (``lub_limit_list_d``, ``lub_limit_ths``, ...) — the addresses where
        DESIGN says the data should land, not just in ``tushare_cache_blob``.

        Strategy:
          - For idempotency, when ``key_cols`` is given, DELETE rows whose
            (key_cols) appear in ``df`` first, then INSERT.
          - Without ``key_cols``, INSERT only (caller responsibility).

        Returns the row count written.
        """
        if df is None or df.empty:
            return 0

        # Verify the table exists; if not, refuse — the strategy plugin (or core
        # migrations) should have created it.
        existing_tables = {
            r[0]
            for r in self._db.fetchall(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            )
        }
        if table_name not in existing_tables:
            raise TushareError(f"materialize target table {table_name!r} does not exist")

        # Discover destination columns to safely down-select df
        dest_cols = [
            r[0]
            for r in self._db.fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? AND table_schema='main' ORDER BY ordinal_position",
                (table_name,),
            )
        ]
        usable = [c for c in dest_cols if c in df.columns]
        if not usable:
            return 0

        df_proj = df[usable].copy()

        with self._db.transaction():
            if key_cols:
                # Build DELETE WHERE (k1, k2) IN (...)
                key_cols = [k for k in key_cols if k in df_proj.columns]
                if key_cols:
                    # Iterate rows; for small N this is fine. For large N, use
                    # temp-table-based anti-join (future optimization).
                    where = " AND ".join([f'"{k}" = ?' for k in key_cols])
                    for _, row in df_proj[key_cols].iterrows():
                        self._db.execute(
                            f'DELETE FROM "{table_name}" WHERE {where}',  # noqa: S608 — names from schema
                            tuple(row.tolist()),
                        )
            # Bulk INSERT via DuckDB's pandas integration
            self._db.conn.register("__mat_df", df_proj)
            try:
                col_list = ", ".join(f'"{c}"' for c in usable)
                self._db.execute(
                    f'INSERT INTO "{table_name}" ({col_list}) '  # noqa: S608
                    f"SELECT {col_list} FROM __mat_df"
                )
            finally:
                self._db.conn.unregister("__mat_df")
        return len(df_proj)

    # --- public entry --------------------------------------------------

    def call(
        self,
        api_name: str,
        *,
        trade_date: str | None = None,
        params: dict[str, Any] | None = None,
        fields: str | None = None,
        force_sync: bool = False,
    ) -> pd.DataFrame:
        """Fetch from cache (if fresh) or transport. See module docstring."""
        params = dict(params or {})
        if trade_date is not None:
            params.setdefault("trade_date", trade_date)
        # F-C1 fix — discriminating cache_key_date that captures windows too,
        # so that daily(start=A,end=B) and daily(start=C,end=D) live in
        # different cache rows even when neither passes a single trade_date.
        cache_key_date = self._compute_cache_key_date(trade_date, params)
        cache_class = API_CACHE_CLASS.get(api_name, "trade_day_immutable")

        state = self._read_state(api_name, cache_key_date)
        if not force_sync and self._cache_hit(
            state, cache_class, api_name=api_name, trade_date=cache_key_date, params=params
        ):
            df = self._read_cached(api_name, cache_key_date, params, fields=None)
        else:
            # ⚠ Bug fix: always fetch the FULL payload from upstream, never let
            # `fields=` constrain what gets cached. Otherwise a caller asking for
            # `fields="ts_code"` would poison the cache with a 1-column frame
            # that all later callers receive.
            df = self._fetch_and_store(api_name, cache_key_date, params, cache_class)

        # Apply field projection at the read site (cache stays full).
        return self._project_fields(df, fields)

    @staticmethod
    def _compute_cache_key_date(trade_date: str | None, params: dict[str, Any]) -> str:
        """Pick a cache_key_date that uniquely partitions queries by date scope.

        Priority:
            1. explicit ``trade_date`` argument or ``params['trade_date']``
            2. ``params['start_date']:params['end_date']`` window key
            3. literal '*' (parameter-less APIs like stock_basic / trade_cal)

        Combined with ``params_hash`` in the payload table, this guarantees that
        e.g. ``daily(start=20260401,end=20260410)`` and
        ``daily(start=20260420,end=20260427)`` cannot collide.
        """
        if trade_date is not None:
            return str(trade_date)
        if "trade_date" in params:
            return str(params["trade_date"])
        start = params.get("start_date")
        end = params.get("end_date")
        if start is not None and end is not None:
            return f"{start}:{end}"
        if start is not None:
            return f"{start}:"
        if end is not None:
            return f":{end}"
        return "*"

    @staticmethod
    def _project_fields(df: pd.DataFrame, fields: str | None) -> pd.DataFrame:
        if fields is None or df is None or df.empty:
            return df
        wanted = [c.strip() for c in fields.split(",") if c.strip()]
        present = [c for c in wanted if c in df.columns]
        if not present:
            return df
        return df[present].copy()

    # --- cache decisions ----------------------------------------------

    def _cache_hit(
        self,
        state: SyncState | None,
        cache_class: CacheClass,
        *,
        api_name: str | None = None,
        trade_date: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> bool:
        if state is None or state.status != "ok":
            return False
        # F4: daily-mode reader rejects intraday-cached data
        if not self._intraday and state.data_completeness == "intraday":
            return False
        # B2.4: state.ok with no payload row is NOT a hit (atomicity defense)
        if api_name is not None and trade_date is not None:
            if not self._payload_exists(api_name, trade_date, params or {}):
                return False
        # By cache class
        if cache_class == "static":
            return self._is_fresh_ttl(state, STATIC_TTL_SECONDS)
        if cache_class == "trade_day_immutable":
            return True
        if cache_class == "trade_day_mutable":
            return not self._is_T_or_T_plus_1(state.trade_date)
        if cache_class == "hot_or_anns":
            ttl = state.ttl_seconds or DEFAULT_HOT_TTL_SECONDS
            return self._is_fresh_ttl(state, ttl)
        return False

    def _payload_exists(self, api_name: str, trade_date: str, params: dict[str, Any]) -> bool:
        """STRICT payload presence check on (plugin_id, api_name, trade_date,
        params_hash)."""
        if not self._cache_table_exists():
            return False
        body = json.dumps(params, sort_keys=True, default=str)
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        row = self._db.fetchone(
            "SELECT 1 FROM tushare_cache_blob "
            "WHERE plugin_id = ? AND api_name = ? AND trade_date = ? AND params_hash = ? "
            "LIMIT 1",
            (self._plugin_id, api_name, trade_date, h),
        )
        return row is not None

    @staticmethod
    def _is_fresh_ttl(state: SyncState, ttl_seconds: int) -> bool:
        return (datetime.now() - state.synced_at) < timedelta(seconds=ttl_seconds)

    @staticmethod
    def _is_T_or_T_plus_1(trade_date: str) -> bool:
        if not trade_date or trade_date == "*":
            return False
        try:
            d = datetime.strptime(trade_date, "%Y%m%d").date()
        except ValueError:
            return False
        today = datetime.now().date()
        return today - d <= timedelta(days=1)

    # --- fetch + store -------------------------------------------------

    def _fetch_and_store(
        self,
        api_name: str,
        cache_key_date: str,
        params: dict[str, Any],
        cache_class: CacheClass,
    ) -> pd.DataFrame:
        try:
            # NOTE: never pass `fields=` to the transport — caching must always
            # store the full payload so different callers requesting different
            # field projections all share one cache entry.
            df = self._fetch_with_retries(api_name, params)
        except TushareUnauthorizedError as e:
            self._write_state(
                api_name,
                cache_key_date,
                "unauthorized",
                row_count=None,
                cache_class=cache_class,
            )
            self._audit_call(api_name, params, rows=0, latency_ms=0)
            raise e
        except TushareServerError as e:
            # B1.4 — 5xx fallback: try local cached payload if state allows.
            existing = self._read_state(api_name, cache_key_date)
            if can_fallback(existing, cache_key_date, is_intraday_run=self._intraday):
                logger.warning(
                    "tushare 5xx for %s @ %s; falling back to cached payload",
                    api_name,
                    cache_key_date,
                )
                cached = self._read_cached(api_name, cache_key_date, params, fields=None)
                # F-L2 — surface fallback to runner / dashboard so users can see
                # that data is being served from cache instead of fresh.
                if self._event_cb is not None:
                    try:
                        self._event_cb(
                            "tushare.fallback",
                            f"tushare 5xx; serving cached payload for {api_name}",
                            {
                                "api_name": api_name,
                                "cache_key_date": cache_key_date,
                                "row_count": len(cached),
                            },
                        )
                    except Exception:  # noqa: BLE001 — never let observers crash a fetch
                        logger.exception("event_cb raised on TUSHARE_FALLBACK")
                # Don't change sync_state — it's still 'ok' with the original payload.
                return cached
            # No usable cache → propagate; caller decides terminate (required) vs degrade (optional)
            raise e

        completeness = self._completeness_for(api_name)
        ttl = DEFAULT_HOT_TTL_SECONDS if cache_class == "hot_or_anns" else None
        # B2.4 — atomic write of state + payload so a partial write can't yield
        # "state=ok but payload missing" stale cache hits.
        with self._db.transaction():
            self._write_state(
                api_name,
                cache_key_date,
                "ok",
                row_count=len(df),
                cache_class=cache_class,
                ttl_seconds=ttl,
                data_completeness=completeness,
            )
            self._write_cached(api_name, cache_key_date, params, df)
        return df

    def _fetch_with_retries(self, api_name: str, params: dict[str, Any]) -> pd.DataFrame:
        """Fetch with tenacity retry around `_do_fetch`. See R1 in __init__."""
        return self._retrying(self._do_fetch, api_name, params)

    def _do_fetch(self, api_name: str, params: dict[str, Any]) -> pd.DataFrame:
        """Fetch the widest payload we'd ever want for this API.

        For most APIs tushare returns every column when ``fields`` is omitted,
        but a few (notably ``stock_basic``) only return a narrow default subset.
        ``WIDE_FIELDS`` overrides ``fields=`` per-API so the cache row contains
        every column downstream callers need; ``call()``'s ``_project_fields``
        narrows it back at READ time.

        First line is `self._bucket.acquire()` — see R1 in `__init__`.
        """
        self._bucket.acquire()
        t0 = time.monotonic()
        try:
            df = self._transport.call(api_name, params, fields=WIDE_FIELDS.get(api_name))
        except TushareRateLimitError:
            self._bucket.decay(0.5)
            raise
        latency_ms = int((time.monotonic() - t0) * 1000)
        self._audit_call(api_name, params, rows=len(df), latency_ms=latency_ms)
        return df

    def _completeness_for(self, api_name: str) -> str:
        if self._intraday and api_name in INTRADAY_SENSITIVE_APIS:
            return "intraday"
        return "final"

    # --- DB read/write helpers ---------------------------------------

    def _read_state(self, api_name: str, trade_date: str) -> SyncState | None:
        row = self._db.fetchone(
            "SELECT plugin_id, api_name, trade_date, status, row_count, cache_class, "
            "ttl_seconds, data_completeness, synced_at FROM tushare_sync_state "
            "WHERE plugin_id = ? AND api_name = ? AND trade_date = ?",
            (self._plugin_id, api_name, trade_date),
        )
        if row is None:
            return None
        return SyncState(
            plugin_id=row[0],
            api_name=row[1],
            trade_date=row[2],
            status=row[3],
            row_count=row[4],
            cache_class=row[5],
            ttl_seconds=row[6],
            data_completeness=row[7],
            synced_at=row[8]
            if isinstance(row[8], datetime)
            else datetime.fromisoformat(str(row[8])),
        )

    def _write_state(
        self,
        api_name: str,
        trade_date: str,
        status: str,
        *,
        row_count: int | None,
        cache_class: CacheClass,
        ttl_seconds: int | None = None,
        data_completeness: str = "final",
    ) -> None:
        with self._db.transaction():
            self._db.execute(
                "DELETE FROM tushare_sync_state "
                "WHERE plugin_id = ? AND api_name = ? AND trade_date = ?",
                (self._plugin_id, api_name, trade_date),
            )
            self._db.execute(
                "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status, "
                "row_count, cache_class, ttl_seconds, data_completeness, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    self._plugin_id,
                    api_name,
                    trade_date,
                    status,
                    row_count,
                    cache_class,
                    ttl_seconds,
                    data_completeness,
                ),
            )

    def _audit_call(
        self, api_name: str, params: dict[str, Any], *, rows: int, latency_ms: int
    ) -> None:
        body = json.dumps(params, sort_keys=True, default=str)
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self._db.execute(
            "INSERT INTO tushare_calls(plugin_id, api_name, params_hash, rows, latency_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._plugin_id, api_name, h, rows, latency_ms),
        )

    # ---- cached frame storage ---------------------------------------
    # We use a generic JSON column so V0.3 doesn't require per-API tables.
    # V0.7a will overlay strategy-specific lub_* tables on top of these.

    def _cache_table_exists(self) -> bool:
        rows = self._db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='tushare_cache_blob'"
        )
        return bool(rows)

    def _ensure_cache_table(self) -> None:
        if self._cache_table_exists():
            return
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS tushare_cache_blob ("
            "  plugin_id VARCHAR NOT NULL,"
            "  api_name VARCHAR NOT NULL,"
            "  trade_date VARCHAR NOT NULL,"
            "  params_hash VARCHAR NOT NULL,"
            "  payload_json VARCHAR NOT NULL,"
            "  cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "  PRIMARY KEY (plugin_id, api_name, trade_date, params_hash)"
            ")"
        )

    # Payload wrapper format (v0.4.1+):
    #   {"version": 1, "schema": {col: dtype_str}, "data": [...records...]}
    # Bypasses pd.read_json's date-column heuristic (which warns on string
    # columns named like dates — trade_date / cal_date / ann_date) and
    # restores dtypes explicitly from the recorded schema. Pre-v0.4.1 rows
    # were a bare records array; those are wiped by core migration
    # 20260512_001_drop_legacy_tushare_cache.sql, so no legacy branch here.
    _CACHE_PAYLOAD_VERSION = 1

    def _write_cached(
        self,
        api_name: str,
        trade_date: str,
        params: dict[str, Any],
        df: pd.DataFrame,
    ) -> None:
        self._ensure_cache_table()
        body = json.dumps(params, sort_keys=True, default=str)
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        records_json = df.to_json(orient="records", date_format="iso")
        payload = json.dumps(
            {
                "version": self._CACHE_PAYLOAD_VERSION,
                "schema": {col: str(dt) for col, dt in df.dtypes.items()},
                "data": json.loads(records_json) if records_json else [],
            }
        )
        with self._db.transaction():
            self._db.execute(
                "DELETE FROM tushare_cache_blob "
                "WHERE plugin_id = ? AND api_name = ? AND trade_date = ? AND params_hash = ?",
                (self._plugin_id, api_name, trade_date, h),
            )
            self._db.execute(
                "INSERT INTO tushare_cache_blob(plugin_id, api_name, trade_date, params_hash, "
                "payload_json) VALUES (?, ?, ?, ?, ?)",
                (self._plugin_id, api_name, trade_date, h, payload),
            )

    def _read_cached(
        self,
        api_name: str,
        trade_date: str,
        params: dict[str, Any],
        fields: str | None,
    ) -> pd.DataFrame:
        if not self._cache_table_exists():
            return pd.DataFrame()
        body = json.dumps(params, sort_keys=True, default=str)
        h = hashlib.sha256(body.encode("utf-8")).hexdigest()
        row = self._db.fetchone(
            "SELECT payload_json FROM tushare_cache_blob "
            "WHERE plugin_id = ? AND api_name = ? AND trade_date = ? AND params_hash = ?",
            (self._plugin_id, api_name, trade_date, h),
        )
        if row is None:
            return pd.DataFrame()
        wrapper = json.loads(row[0])
        df = self._restore_cached_frame(wrapper)
        if fields:
            cols = [c.strip() for c in fields.split(",") if c.strip() in df.columns]
            df = df[cols]
        return df

    @classmethod
    def _restore_cached_frame(cls, wrapper: dict[str, Any]) -> pd.DataFrame:
        schema: dict[str, str] = wrapper["schema"]
        data: list[dict[str, Any]] = wrapper["data"]
        df = pd.DataFrame.from_records(data, columns=list(schema.keys()))
        for col, dtype_str in schema.items():
            if col not in df.columns:
                continue
            if dtype_str.startswith("datetime"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
            elif dtype_str == "object":
                continue
            else:
                try:
                    df[col] = df[col].astype(dtype_str)
                except (TypeError, ValueError):
                    # Best-effort: if a numeric/bool column can't be coerced
                    # back (e.g. all-null), leave the inferred dtype.
                    pass
        return df


# ---------------------------------------------------------------------------
# Fallback predicate (DESIGN §13.2 + S4)
# ---------------------------------------------------------------------------


def can_fallback(
    state: SyncState | None,
    target_trade_date: str,
    *,
    is_intraday_run: bool,
) -> bool:
    """Decide if a 5xx/timeout failure may use already-cached data.

    Conditions (all required):
        - state.status == 'ok'
        - state.trade_date == target_trade_date  (no nearest-day approximation)
        - cache_class != trade_day_mutable when target is T or T+1
        - row_count >= 0 (zero rows ARE valid — S4 fix)
        - data_completeness == 'final' for daily-mode runs (F4)
    """
    if state is None or state.status != "ok":
        return False
    if state.trade_date != target_trade_date:
        return False
    if state.row_count is not None and state.row_count < 0:
        return False
    if (
        state.cache_class == "trade_day_mutable"
        and TushareClient._is_T_or_T_plus_1(target_trade_date)  # noqa: SLF001 — internal helper reuse
    ):
        return False
    if not is_intraday_run and state.data_completeness == "intraday":
        return False
    return True
