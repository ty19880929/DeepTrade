-- v0.8 — Phase B1: 龙虎榜接入。
-- top_list / top_inst 在本插件中升级为 required（账户已具权限）。
-- candidate 未上榜时 lhb_* 字段为 null（合法事实），不进 data_unavailable。

CREATE TABLE IF NOT EXISTS lub_top_list (
    trade_date     VARCHAR NOT NULL,
    ts_code        VARCHAR NOT NULL,
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
    reason         VARCHAR,
    PRIMARY KEY (trade_date, ts_code)
);

CREATE TABLE IF NOT EXISTS lub_top_inst (
    trade_date  VARCHAR NOT NULL,
    ts_code     VARCHAR NOT NULL,
    exalter     VARCHAR NOT NULL,
    side        INTEGER NOT NULL,            -- 0 = buy, 1 = sell
    buy         DOUBLE,
    buy_rate    DOUBLE,
    sell        DOUBLE,
    sell_rate   DOUBLE,
    net_buy     DOUBLE,
    reason      VARCHAR,
    PRIMARY KEY (trade_date, ts_code, exalter, side)
);
