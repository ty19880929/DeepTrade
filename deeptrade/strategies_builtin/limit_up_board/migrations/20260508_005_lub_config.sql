-- v0.4 — plugin-local settings store for limit-up-board.
--
-- Holds user-tunable run filters (流通市值 / 当前股价 上限). Distinct from the
-- framework-level app_config table: framework keys are namespaced by the
-- whitelisted AppConfig schema; per-plugin tunables live here so the framework
-- stays free of plugin-specific knobs (Plan A pure isolation).
--
-- Keys currently in use (defaults are owned by limit_up_board.config:LubConfig
-- and re-applied automatically when a row is missing — no DEFAULT here):
--     lub.max_float_mv_yi   — max 流通市值 in 亿
--     lub.max_close_yuan    — max 当前股价 in 元

CREATE TABLE IF NOT EXISTS lub_config (
    key         VARCHAR PRIMARY KEY,
    value_json  VARCHAR NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
