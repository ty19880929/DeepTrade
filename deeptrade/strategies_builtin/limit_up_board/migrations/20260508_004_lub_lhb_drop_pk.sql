-- v0.8 Phase B1 fix #2 — drop PRIMARY KEY on lub_top_list / lub_top_inst.
--
-- 20260508_003 widened the PK to include `reason`, but Tushare data still has
-- legitimate duplicates beyond the natural composite key:
--
--   * top_inst: anonymous institutional seats are reported as exalter="机构专用"
--     (or "深股通专用" / "沪股通专用"). A single LHB list has up to 5 buy seats
--     and 5 sell seats; multiple slots on the same side can all show as
--     "机构专用" because the actual identity is hidden — Tushare emits each as
--     a separate row with identical (trade_date, ts_code, exalter, side, reason).
--     This is legitimate multi-seat semantics, NOT bad data.
--
--   * top_list: some upstream-pushed rows arrive duplicated on the natural
--     key (e.g. 688755.SH 融资类规则). The reason field doesn't always fully
--     distinguish.
--
-- DB-enforced uniqueness was the wrong abstraction for these tables. Drop the
-- PK; rely on materialize()'s natural-key DELETE → INSERT for idempotency, keep
-- NOT NULL on the natural-key columns for data quality.

DROP TABLE IF EXISTS lub_top_list;
DROP TABLE IF EXISTS lub_top_inst;

CREATE TABLE lub_top_list (
    trade_date     VARCHAR NOT NULL,
    ts_code        VARCHAR NOT NULL,
    reason         VARCHAR NOT NULL,
    name           VARCHAR,
    close          DOUBLE,
    pct_change     DOUBLE,
    turnover_rate  DOUBLE,
    amount         DOUBLE,
    l_sell         DOUBLE,
    l_buy          DOUBLE,
    l_amount       DOUBLE,
    net_amount     DOUBLE,
    net_rate       DOUBLE,
    amount_rate    DOUBLE,
    float_values   DOUBLE
);

CREATE TABLE lub_top_inst (
    trade_date  VARCHAR NOT NULL,
    ts_code     VARCHAR NOT NULL,
    exalter     VARCHAR NOT NULL,
    side        INTEGER NOT NULL,            -- 0 = buy, 1 = sell
    reason      VARCHAR NOT NULL,
    buy         DOUBLE,
    buy_rate    DOUBLE,
    sell        DOUBLE,
    sell_rate   DOUBLE,
    net_buy     DOUBLE
);
