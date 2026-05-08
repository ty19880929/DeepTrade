-- volume-anomaly v0.4.0 — T+N realized-returns table.
--
-- Stores per-anomaly post-event prices and computed returns for the
-- (T+1, T+3, T+5, T+10) horizons plus 5d/10d windowed extremes. Filled
-- by the new `evaluate` CLI subcommand and consumed by `stats`.
--
-- PK = (anomaly_date, ts_code) — one row per (T, candidate) pair across
-- the entire history of va_anomaly_history (G3 decision: evaluate the full
-- hit history, not just rows that also appear in va_stage_results).

CREATE TABLE IF NOT EXISTS va_realized_returns (
    anomaly_date    VARCHAR NOT NULL,
    ts_code         VARCHAR NOT NULL,
    -- T-day close (G9: redundant store, lets stats run independently of
    -- va_anomaly_history and survives accidental row deletions there).
    t_close         DOUBLE,
    -- Per-horizon close (NULL when the trade_date is in the future or the
    -- stock was suspended that day).
    t1_close        DOUBLE,
    t3_close        DOUBLE,
    t5_close        DOUBLE,
    t10_close       DOUBLE,
    -- Per-horizon return: ret = (tn_close / t_close - 1) × 100. NULL when
    -- either close is missing.
    ret_t1          DOUBLE,
    ret_t3          DOUBLE,
    ret_t5          DOUBLE,
    ret_t10         DOUBLE,
    -- Windowed extremes — captures peak / trough over T+1..T+5 and T+1..T+10
    -- so we can measure realised launches that resolve before the horizon.
    max_close_5d    DOUBLE,
    max_close_10d   DOUBLE,
    max_ret_5d      DOUBLE,
    max_ret_10d     DOUBLE,
    -- G2 决策: max_dd computed from T (i.e. (min(close[T+1..T+5]) - t_close) / t_close × 100).
    max_dd_5d       DOUBLE,
    -- Bookkeeping
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    data_status     VARCHAR NOT NULL,    -- 'pending' | 'partial' | 'complete'
    PRIMARY KEY (anomaly_date, ts_code)
);

CREATE INDEX IF NOT EXISTS idx_va_realized_returns_date
    ON va_realized_returns(anomaly_date, data_status);
