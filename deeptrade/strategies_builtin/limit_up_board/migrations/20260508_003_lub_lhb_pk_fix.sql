-- v0.8 Phase B1 fix — top_list/top_inst PRIMARY KEY needs to include `reason`.
--
-- Tushare returns multiple rows per (trade_date, ts_code) when a stock triggers
-- the LHB for several reasons on the same day (e.g. 日涨幅偏离值达 7% +
-- 连续三日累计偏离值达 20%). top_inst is the same: the same (date, code,
-- exalter, side) can appear under several reasons.
--
-- 20260508_001 defined PKs that were narrower than the natural uniqueness key,
-- so the first real materialize hit ConstraintException. Drop & recreate —
-- these are tushare-derived caches, no business data loss; next run repopulates.

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
    float_values   DOUBLE,
    PRIMARY KEY (trade_date, ts_code, reason)
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
    net_buy     DOUBLE,
    PRIMARY KEY (trade_date, ts_code, exalter, side, reason)
);
