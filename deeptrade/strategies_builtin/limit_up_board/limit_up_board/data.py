"""Data layer for the limit-up-board strategy.

DESIGN §12.2 (T-resolution) + §11.3 (sector_strength fallback chain) + S2 (close_after config) +
S4 (zero candidates legal) + Q2 (main board only) + C5 (raw units in DB, normalized in prompt).

Key public entry points:
    resolve_trade_date(...)            — Step 0
    collect_round1(...)                — Step 1 (returns candidates + market summary +
                                          sector_strength + data_unavailable)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Literal

import pandas as pd

from deeptrade.core.tushare_client import (
    TushareClient,
    TushareUnauthorizedError,
)

from .calendar import TradeCalendar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 0 — resolve trade date
# ---------------------------------------------------------------------------


def resolve_trade_date(
    now_dt: datetime,
    calendar: TradeCalendar,
    *,
    user_specified: str | None = None,
    allow_intraday: bool = False,
    close_after: time = time(18, 0),
) -> tuple[str, str]:
    """Return (T, T+1) per DESIGN §12.2.

    T defaults to the most recent CLOSED trade day:
      * if today is open AND now ≥ close_after  → today
      * if today is open AND allow_intraday      → today (with intraday banner)
      * else                                     → pretrade_date(today)

    T+1 is the first open day strictly after T.
    """
    if user_specified:
        T = user_specified
        return T, calendar.next_open(T)

    today = now_dt.strftime("%Y%m%d")
    today_is_open = calendar.is_open(today)

    if today_is_open and (now_dt.time() >= close_after or allow_intraday):
        T = today
    elif today_is_open:
        # Today is a trade day but it's intraday and user has not opted in.
        T = calendar.pretrade_date(today)
    else:
        # Non-trading day (weekend/holiday). Walk back.
        T = calendar.pretrade_date(today)

    return T, calendar.next_open(T)


# ---------------------------------------------------------------------------
# Filters: main board / ST / suspended
# ---------------------------------------------------------------------------


def main_board_filter(stock_basic: pd.DataFrame) -> pd.DataFrame:
    """Keep only Shanghai/Shenzhen MAIN board (Q2 fix).

    Excludes ChiNext (300xxx), STAR (688xxx), BSE (8xxxxx), and CDR.
    Tushare ``stock_basic.market`` is a Chinese label like '主板'.
    """
    if "market" not in stock_basic.columns or "exchange" not in stock_basic.columns:
        raise ValueError("stock_basic missing market/exchange columns")
    df = stock_basic[
        (stock_basic["market"] == "主板") & (stock_basic["exchange"].isin(["SSE", "SZSE"]))
    ].copy()
    if "list_status" in df.columns:
        df = df[df["list_status"] == "L"]
    return df.reset_index(drop=True)


def exclude_st(df: pd.DataFrame, st_codes: set[str]) -> pd.DataFrame:
    """Drop rows whose ts_code is in the ST / *ST set."""
    if df.empty:
        return df
    return df[~df["ts_code"].isin(st_codes)].reset_index(drop=True)


def exclude_suspended(df: pd.DataFrame, suspended_codes: set[str]) -> pd.DataFrame:
    """Drop rows whose ts_code is suspended on T."""
    if df.empty:
        return df
    return df[~df["ts_code"].isin(suspended_codes)].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sector strength resolver — three-tier fallback (F2 fix + §11.3)
# ---------------------------------------------------------------------------


SectorStrengthSource = Literal["limit_cpt_list", "lu_desc_aggregation", "industry_fallback"]


@dataclass
class SectorStrength:
    """Sector heat / leadership data fed into the prompt.

    `source` is exposed verbatim to the LLM via ``sector_strength_source`` so
    the model can downweight confidence when it sees a fallback label.
    """

    source: SectorStrengthSource
    data: dict[str, Any]


def resolve_sector_strength(
    *,
    candidates: pd.DataFrame,
    limit_cpt_list: pd.DataFrame | None,
    limit_list_ths: pd.DataFrame | None,
) -> SectorStrength:
    """Pick the best available sector data and aggregate by candidate's sector tag.

    Priority: limit_cpt_list > limit_list_ths.lu_desc aggregation >
    stock_basic.industry aggregation.
    """
    # Tier 1: official concept rankings
    if limit_cpt_list is not None and not limit_cpt_list.empty:
        # Top-ranked sectors (rank ascending, take first ~10)
        top = limit_cpt_list.sort_values("rank").head(10)
        return SectorStrength(
            source="limit_cpt_list",
            data={
                "top_sectors": top.to_dict(orient="records"),
                "candidates_with_sector_tag": [],  # joined externally if needed
            },
        )

    # Tier 2: aggregate THS涨停原因
    if limit_list_ths is not None and not limit_list_ths.empty:
        agg = (
            limit_list_ths.groupby("lu_desc", dropna=True)
            .agg(up_nums=("ts_code", "count"))
            .reset_index()
            .sort_values("up_nums", ascending=False)
            .head(10)
        )
        return SectorStrength(
            source="lu_desc_aggregation",
            data={"top_sectors": agg.to_dict(orient="records")},
        )

    # Tier 3: aggregate by stock_basic.industry  (last resort)
    if candidates is not None and not candidates.empty and "industry" in candidates.columns:
        agg = (
            candidates.groupby("industry", dropna=True)
            .agg(up_nums=("ts_code", "count"))
            .reset_index()
            .sort_values("up_nums", ascending=False)
            .head(10)
        )
        return SectorStrength(
            source="industry_fallback",
            data={"top_sectors": agg.to_dict(orient="records")},
        )

    return SectorStrength(source="industry_fallback", data={"top_sectors": []})


# ---------------------------------------------------------------------------
# Normalizers (C5 fix: prompt uses normalized units; DB keeps raw)
# B3.1 (M6) fix: tushare fields have HETEROGENEOUS raw units; a simple
# `value / 1e8` is wrong for moneyflow.* (which is 万元) and daily_basic.circ_mv
# (also 万元). FIELD_UNITS_RAW is the source of truth.
# ---------------------------------------------------------------------------


# Per-field raw unit declarations, sourced from tushare official docs.
# Values absent from this map default to "元" (the most common unit).
FIELD_UNITS_RAW: dict[str, str] = {
    # limit_list_d (元)
    "fd_amount": "元",
    "limit_amount": "元",
    "amount": "元",
    "float_mv": "元",
    "total_mv": "元",
    # daily_basic (mixed: market values are 万元 in tushare!)
    "circ_mv": "万元",
    "free_share": "万股",
    "float_share": "万股",
    "total_share": "万股",
    # moneyflow (all amounts in 万元)
    "net_mf_amount": "万元",
    "buy_lg_amount": "万元",
    "buy_elg_amount": "万元",
    "buy_md_amount": "万元",
    "buy_sm_amount": "万元",
    "sell_lg_amount": "万元",
    "sell_elg_amount": "万元",
    # daily (千元 for amount, 手 for vol)
    # Note: limit_list_d.amount is 元 but daily.amount is 千元 — context-dependent
    # callers must use normalize_field with the API context if they need disambiguation.
}


def normalize_to_yi(field: str, raw_value: float | None) -> float | None:
    """Convert a raw field value to 亿 based on its declared unit."""
    if raw_value is None or pd.isna(raw_value):
        return None
    unit = FIELD_UNITS_RAW.get(field, "元")
    if unit == "元":
        factor = 1e8
    elif unit == "万元":
        factor = 1e4
    elif unit == "千元":
        factor = 1e5
    else:
        return None
    return round(float(raw_value) / factor, 2)


def normalize_to_wan(field: str, raw_value: float | None) -> float | None:
    """Convert a raw field value to 万 based on its declared unit."""
    if raw_value is None or pd.isna(raw_value):
        return None
    unit = FIELD_UNITS_RAW.get(field, "元")
    if unit == "元":
        factor = 1e4
    elif unit == "万元":
        factor = 1.0
    elif unit == "千元":
        factor = 0.1
    else:
        return None
    return round(float(raw_value) / factor, 2)


def yi(value: float | None) -> float | None:
    """Legacy helper assuming raw='元'. Prefer ``normalize_to_yi(field, value)``."""
    if value is None or pd.isna(value):
        return None
    return round(float(value) / 1e8, 2)


def wan(value: float | None) -> float | None:
    """Legacy helper assuming raw='元'. Prefer ``normalize_to_wan(field, value)``."""
    if value is None or pd.isna(value):
        return None
    return round(float(value) / 1e4, 2)


def round2(value: float | None) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)


# ---------------------------------------------------------------------------
# Round-1 collection
# ---------------------------------------------------------------------------


@dataclass
class Round1Bundle:
    """Everything the R1 LLM stage needs."""

    trade_date: str
    next_trade_date: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    market_summary: dict[str, Any] = field(default_factory=dict)
    sector_strength: SectorStrength = field(
        default_factory=lambda: SectorStrength(source="industry_fallback", data={"top_sectors": []})
    )
    data_unavailable: list[str] = field(default_factory=list)


def collect_round1(
    *,
    tushare: TushareClient,
    trade_date: str,
    next_trade_date: str,
    daily_lookback: int = 10,
    moneyflow_lookback: int = 5,
    force_sync: bool = False,
) -> Round1Bundle:
    """Assemble the R1 input bundle.

    The flow:
        1. stock_basic (static) → main_board_filter()
        2. limit_list_d(T, limit='U') → join main_board → DROP if 0 candidates
           (zero candidates is a LEGAL outcome — S4)
        3. stock_st(T) (REQUIRED) / suspend_d(T) (optional) → drop codes
        4. limit_list_ths(T) (optional) → bring in lu_desc, tag, suc_rate
        5. limit_cpt_list(T) (optional) → sector strength tier 1
        6. limit_step(T) (REQUIRED) — for global ladder distribution
        7. daily / daily_basic / moneyflow over T-N..T (B1.2): histories that
           let the LLM see trend, turnover, market value, capital flow
        8. Build normalized prompt fields per candidate (raw → normalized via FIELD_UNITS_RAW)
    """
    bundle = Round1Bundle(trade_date=trade_date, next_trade_date=next_trade_date)
    data_unavailable: list[str] = []

    # 1. main board pool
    stock_basic = tushare.call("stock_basic", force_sync=force_sync)
    main_pool = main_board_filter(stock_basic)

    # 2. limit-up rows (limit='U'); we filter by limit afterward in case the
    # transport returns the full list_d.
    limit_list_d = tushare.call(
        "limit_list_d",
        trade_date=trade_date,
        params={"limit_type": "U"},
        force_sync=force_sync,
    )
    if "limit" in limit_list_d.columns:
        limit_list_d = limit_list_d[limit_list_d["limit"] == "U"]

    # join on ts_code
    if limit_list_d.empty:
        bundle.candidates = []
        return bundle  # zero candidates: legal end state (S4)
    candidates_df = limit_list_d.merge(
        main_pool[["ts_code", "market", "exchange", "industry", "list_date"]].rename(
            columns={"industry": "industry_basic"}
        ),
        on="ts_code",
        how="inner",
    )
    if candidates_df.empty:
        bundle.candidates = []
        return bundle

    # 3a. ST exclusion — REQUIRED. Unauthorized must propagate to the runner.
    # Per DESIGN §11.1 + B1.3 fix: stock_st is in metadata.required → cannot
    # be silently skipped; runner will mark the run failed.
    st_df = tushare.call("stock_st", trade_date=trade_date, force_sync=force_sync)
    st_codes = set(st_df["ts_code"].astype(str)) if not st_df.empty else set()
    candidates_df = exclude_st(candidates_df, st_codes)

    # 3b. Suspended exclusion — OPTIONAL. F-H3: catch all transient errors.
    susp_df, susp_err = _try_optional(
        tushare, "suspend_d", trade_date=trade_date, force_sync=force_sync
    )
    if susp_err:
        data_unavailable.append(f"suspend_d ({susp_err})")
        susp_codes: set[str] = set()
    else:
        susp_codes = set(susp_df["ts_code"].astype(str)) if not susp_df.empty else set()
    candidates_df = exclude_suspended(candidates_df, susp_codes)

    if candidates_df.empty:
        bundle.candidates = []
        return bundle

    # 4. THS涨停榜 (optional). F-H3: catch all transient errors.
    ths_df, ths_err = _try_optional(
        tushare,
        "limit_list_ths",
        trade_date=trade_date,
        params={"limit_type": "U"},
        force_sync=force_sync,
    )
    if ths_err:
        data_unavailable.append(f"limit_list_ths ({ths_err})")

    # 5. concept ranking (optional). F-H3: same.
    cpt_df, cpt_err = _try_optional(
        tushare, "limit_cpt_list", trade_date=trade_date, force_sync=force_sync
    )
    if cpt_err:
        data_unavailable.append(f"limit_cpt_list ({cpt_err})")

    sector = resolve_sector_strength(
        candidates=candidates_df,
        limit_cpt_list=cpt_df,
        limit_list_ths=ths_df,
    )
    bundle.sector_strength = sector

    # 6. limit_step (required) — for global ladder distribution
    step_df = tushare.call("limit_step", trade_date=trade_date, force_sync=force_sync)
    bundle.market_summary = {
        "limit_up_count": int(len(candidates_df)),
        "limit_step_distribution": _summarize_limit_step(step_df),
    }

    # 7. B1.2 — REQUIRED histories: daily / daily_basic / moneyflow over a window.
    # Tushare returns ALL stocks for one trade_date in one call; we instead query
    # by trade_date range so each ts_code's history is one slice of the result.
    candidate_codes = set(candidates_df["ts_code"].astype(str))
    start_date = _shift_date(trade_date, -(daily_lookback + 5))  # +5 buffer for non-trade days
    daily_df = _fetch_history_window(
        tushare, "daily", start_date, trade_date, candidate_codes, force_sync=force_sync
    )
    daily_basic_df = _fetch_history_window(
        tushare,
        "daily_basic",
        start_date,
        trade_date,
        candidate_codes,
        force_sync=force_sync,
    )
    mf_start = _shift_date(trade_date, -(moneyflow_lookback + 5))
    moneyflow_df = _fetch_history_window(
        tushare,
        "moneyflow",
        mf_start,
        trade_date,
        candidate_codes,
        force_sync=force_sync,
    )

    # 8. Build normalized rows
    bundle.candidates = _build_candidate_rows(
        candidates_df,
        ths_df,
        daily_df=daily_df,
        daily_basic_df=daily_basic_df,
        moneyflow_df=moneyflow_df,
        daily_lookback=daily_lookback,
        moneyflow_lookback=moneyflow_lookback,
    )
    bundle.data_unavailable = data_unavailable

    # B2.3 + F-M4 — Persist to business tables (DuckDB is the persistence layer
    # per DESIGN). Errors don't fail the run (cache_blob still holds the data),
    # but they DO surface via data_unavailable so users see them in the report.
    materialize_errors = _materialize_business_tables(
        tushare,
        stock_basic=stock_basic,
        limit_list_d=limit_list_d,
        ths_df=ths_df,
        daily_df=daily_df,
        daily_basic_df=daily_basic_df,
        moneyflow_df=moneyflow_df,
    )
    if materialize_errors:
        bundle.data_unavailable.extend(materialize_errors)
    return bundle


def _materialize_business_tables(
    tushare: TushareClient,
    *,
    stock_basic: pd.DataFrame,
    limit_list_d: pd.DataFrame,
    ths_df: pd.DataFrame | None,
    daily_df: pd.DataFrame | None,
    daily_basic_df: pd.DataFrame | None,
    moneyflow_df: pd.DataFrame | None,
) -> list[str]:
    """B2.3 + F-M4 — write tushare frames into the named business tables.

    Returns a list of error strings for any tables that failed to materialize.
    Caller surfaces these via data_unavailable / events instead of silent log.
    """
    errors: list[str] = []

    def _safe(table: str, df: pd.DataFrame, key_cols: list[str]) -> None:
        if df is None or df.empty:
            return
        try:
            tushare.materialize(table, df, key_cols=key_cols)
        except Exception as e:  # noqa: BLE001
            msg = f"materialize:{table} ({type(e).__name__}: {e})"
            logger.warning(msg)
            errors.append(msg)

    # All tables live under the lub_* prefix — this plugin owns its own
    # copy of every tushare-derived business table (Plan A pure isolation).
    _safe("lub_stock_basic", stock_basic, ["ts_code"])
    _safe("lub_limit_list_d", limit_list_d, ["trade_date", "ts_code", "limit"])
    _safe(
        "lub_limit_ths",
        ths_df if ths_df is not None else pd.DataFrame(),
        ["trade_date", "ts_code", "limit_type"],
    )
    _safe(
        "lub_daily",
        daily_df if daily_df is not None else pd.DataFrame(),
        ["ts_code", "trade_date"],
    )
    _safe(
        "lub_daily_basic",
        daily_basic_df if daily_basic_df is not None else pd.DataFrame(),
        ["ts_code", "trade_date"],
    )
    _safe(
        "lub_moneyflow",
        moneyflow_df if moneyflow_df is not None else pd.DataFrame(),
        ["ts_code", "trade_date"],
    )
    return errors


def _shift_date(yyyymmdd: str, days: int) -> str:
    """Naive ±days shift on YYYYMMDD (calendar days, not trade days). Adequate for
    setting a tushare query upper bound; result is filtered by trade_cal anyway."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    d = _dt.strptime(yyyymmdd, "%Y%m%d") + _td(days=days)
    return d.strftime("%Y%m%d")


def _fetch_history_window(
    tushare: TushareClient,
    api_name: str,
    start_date: str,
    end_date: str,
    candidate_codes: set[str],
    *,
    force_sync: bool = False,
) -> pd.DataFrame:
    """Fetch (api_name) for [start_date, end_date]; filter to candidates."""
    # tushare daily/daily_basic/moneyflow accept start_date/end_date for batch fetch.
    df = tushare.call(
        api_name,
        params={"start_date": start_date, "end_date": end_date},
        force_sync=force_sync,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if "ts_code" in df.columns and candidate_codes:
        df = df[df["ts_code"].astype(str).isin(candidate_codes)]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# F-H3 — optional API wrapper
# ---------------------------------------------------------------------------


def _try_optional(
    tushare: TushareClient, api_name: str, **kwargs: Any
) -> tuple[pd.DataFrame, str | None]:
    """Call an optional tushare API; on transient failure return (empty df, err msg).

    Catches: TushareUnauthorizedError, TushareServerError, TushareRateLimitError.
    Required APIs should NOT use this — they should propagate failure.
    """
    from deeptrade.core.tushare_client import (  # noqa: PLC0415
        TushareRateLimitError,
        TushareServerError,
    )

    try:
        return tushare.call(api_name, **kwargs), None
    except TushareUnauthorizedError as e:
        return pd.DataFrame(), f"unauthorized: {e}"
    except TushareServerError as e:
        return pd.DataFrame(), f"server_error: {e}"
    except TushareRateLimitError as e:
        return pd.DataFrame(), f"rate_limited: {e}"


def _summarize_limit_step(step_df: pd.DataFrame) -> dict[str, int]:
    """Convert limit_step rows to a {board_height: count} mapping."""
    if step_df is None or step_df.empty:
        return {}
    if "nums" not in step_df.columns:
        return {}
    counts = step_df.groupby("nums").size().to_dict()
    return {str(k): int(v) for k, v in counts.items()}


def _build_candidate_rows(
    candidates_df: pd.DataFrame,
    ths_df: pd.DataFrame | None,
    *,
    daily_df: pd.DataFrame | None = None,
    daily_basic_df: pd.DataFrame | None = None,
    moneyflow_df: pd.DataFrame | None = None,
    daily_lookback: int = 10,
    moneyflow_lookback: int = 5,
) -> list[dict[str, Any]]:
    """Project candidates to a list of dicts with raw + normalized fields + history.

    B1.2 additions:
        prev_daily        — last N daily rows: [(date, close, pct_chg, vol), ...]
        prev_moneyflow    — last N moneyflow rows: [(date, net_mf_yi, buy_lg_yi, buy_elg_yi)]
        turnover_rate, volume_ratio, circ_mv_yi   — from daily_basic on T

    All numeric fields go through normalize_to_yi/wan with FIELD_UNITS_RAW for
    correct unit conversion (B3.1 / M6 fix).
    """
    if ths_df is not None and not ths_df.empty:
        ths_lookup = ths_df.set_index("ts_code").to_dict(orient="index")
    else:
        ths_lookup = {}

    daily_by_code = _index_by_code(daily_df)
    daily_basic_by_code = _index_by_code(daily_basic_df)
    moneyflow_by_code = _index_by_code(moneyflow_df)

    out: list[dict[str, Any]] = []
    for row in candidates_df.itertuples(index=False):
        ts_code = str(row.ts_code)
        rec = {
            "candidate_id": ts_code,
            "ts_code": ts_code,
            "name": getattr(row, "name", None),
            "industry": getattr(row, "industry_basic", None) or getattr(row, "industry", None),
            "first_time": getattr(row, "first_time", None),
            "last_time": getattr(row, "last_time", None),
            "open_times": _opt_int(getattr(row, "open_times", None)),
            "limit_times": _opt_int(getattr(row, "limit_times", None)),
            "up_stat": getattr(row, "up_stat", None),
            "pct_chg": round2(getattr(row, "pct_chg", None)),
            "turnover_ratio": round2(getattr(row, "turnover_ratio", None)),
            "fd_amount_yi": normalize_to_yi("fd_amount", getattr(row, "fd_amount", None)),
            "limit_amount_yi": normalize_to_yi("limit_amount", getattr(row, "limit_amount", None)),
            "amount_yi": normalize_to_yi("amount", getattr(row, "amount", None)),
            "total_mv_yi": normalize_to_yi("total_mv", getattr(row, "total_mv", None)),
            "float_mv_yi": normalize_to_yi("float_mv", getattr(row, "float_mv", None)),
        }
        ths = ths_lookup.get(ts_code)
        if ths is not None:
            rec["lu_desc"] = ths.get("lu_desc")
            rec["tag"] = ths.get("tag")
            rec["limit_up_suc_rate"] = round2(ths.get("limit_up_suc_rate"))
            rec["free_float_yi"] = normalize_to_yi("free_float", ths.get("free_float"))

        # B1.2 history attachments
        d_hist = daily_by_code.get(ts_code, [])
        if d_hist:
            rec["prev_daily"] = [
                {
                    "date": r.get("trade_date"),
                    "close": round2(r.get("close")),
                    "pct_chg": round2(r.get("pct_chg")),
                    "vol": _opt_int(r.get("vol")),
                }
                for r in d_hist[-daily_lookback:]
            ]
        db_hist = daily_basic_by_code.get(ts_code, [])
        if db_hist:
            latest = db_hist[-1]
            rec["turnover_rate"] = round2(latest.get("turnover_rate"))
            rec["volume_ratio"] = round2(latest.get("volume_ratio"))
            rec["circ_mv_yi"] = normalize_to_yi("circ_mv", latest.get("circ_mv"))
        mf_hist = moneyflow_by_code.get(ts_code, [])
        if mf_hist:
            rec["prev_moneyflow"] = [
                {
                    "date": r.get("trade_date"),
                    "net_mf_yi": normalize_to_yi("net_mf_amount", r.get("net_mf_amount")),
                    "buy_lg_yi": normalize_to_yi("buy_lg_amount", r.get("buy_lg_amount")),
                    "buy_elg_yi": normalize_to_yi("buy_elg_amount", r.get("buy_elg_amount")),
                }
                for r in mf_hist[-moneyflow_lookback:]
            ]
        out.append(rec)
    return out


def _index_by_code(df: pd.DataFrame | None) -> dict[str, list[dict[str, Any]]]:
    """Group a DataFrame by ts_code into ascending-by-trade_date row lists."""
    if df is None or df.empty or "ts_code" not in df.columns:
        return {}
    if "trade_date" in df.columns:
        df = df.sort_values("trade_date")
    out: dict[str, list[dict[str, Any]]] = {}
    for code, group in df.groupby("ts_code"):
        out[str(code)] = group.to_dict(orient="records")
    return out


def _opt_int(v: Any) -> int | None:
    if v is None or pd.isna(v):
        return None
    return int(v)
