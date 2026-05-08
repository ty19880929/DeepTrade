-- v0.8 — Phase B2: 筹码集中度（cyq_perf）接入。
-- 账户已具 cyq_perf 权限，按 required 接入；失败 → run terminated。
-- 单只 candidate 在返回中无记录时该 candidate.missing_data 写入 cyq 字段名。

CREATE TABLE IF NOT EXISTS lub_cyq_perf (
    trade_date  VARCHAR NOT NULL,
    ts_code     VARCHAR NOT NULL,
    his_low     DOUBLE,
    his_high    DOUBLE,
    cost_5pct   DOUBLE,
    cost_15pct  DOUBLE,
    cost_50pct  DOUBLE,
    cost_85pct  DOUBLE,
    cost_95pct  DOUBLE,
    weight_avg  DOUBLE,
    winner_rate DOUBLE,
    PRIMARY KEY (trade_date, ts_code)
);
