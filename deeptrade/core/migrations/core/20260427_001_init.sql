-- DeepTrade core schema initial migration.
--
-- Scope: ONLY framework-owned tables. Plugin-owned tables (including any
-- tushare-derived business tables like stock_basic / daily / moneyflow) are
-- declared by each plugin in its own deeptrade_plugin.yaml + migrations/*.sql
-- and applied via plugin_schema_migrations (per-plugin tracking). The
-- framework never owns business data tables.

-- ============================================================
-- Framework configuration & secrets
-- ============================================================

-- Non-secret app config
CREATE TABLE IF NOT EXISTS app_config (
    key         VARCHAR PRIMARY KEY,
    value_json  VARCHAR NOT NULL,
    is_secret   BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Encrypted secrets (keyring-backed; plaintext fallback when keyring unavailable)
CREATE TABLE IF NOT EXISTS secret_store (
    key                VARCHAR PRIMARY KEY,
    encrypted_value    BLOB    NOT NULL,
    encryption_method  VARCHAR NOT NULL,        -- 'keyring' | 'plaintext'
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Framework schema-migration tracking
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Plugin registry
-- ============================================================

CREATE TABLE IF NOT EXISTS plugins (
    plugin_id     VARCHAR PRIMARY KEY,
    name          VARCHAR NOT NULL,
    version       VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,             -- 'strategy' | 'channel' | future
    api_version   VARCHAR NOT NULL,
    entrypoint    VARCHAR NOT NULL,
    install_path  VARCHAR NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_yaml VARCHAR NOT NULL,
    installed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plugin_tables (
    plugin_id  VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL,
    description VARCHAR,
    purge_on_uninstall BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plugin_id, table_name)
);

CREATE TABLE IF NOT EXISTS plugin_schema_migrations (
    plugin_id   VARCHAR NOT NULL,
    version     VARCHAR NOT NULL,
    checksum    VARCHAR NOT NULL,
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plugin_id, version)
);

-- ============================================================
-- Framework service audit / cache state
-- ============================================================

-- LLM call audit (LLMClient writes; per-plugin scoped via plugin_id column).
-- v0.7 dropped the `stage` column — see migration 20260501_002.
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id           UUID PRIMARY KEY,
    run_id            UUID,
    plugin_id         VARCHAR,
    model             VARCHAR,
    prompt_hash       VARCHAR,
    input_tokens      BIGINT,
    output_tokens     BIGINT,
    latency_ms        INTEGER,
    request_json      VARCHAR,
    response_json     VARCHAR,
    validation_status VARCHAR,
    error             VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tushare sync idempotency state, per (plugin_id, api_name, trade_date).
-- Each plugin tracks its own sync state — plugins do not share cached
-- payloads with each other (per pure-isolation data model).
CREATE TABLE IF NOT EXISTS tushare_sync_state (
    plugin_id          VARCHAR NOT NULL,
    api_name           VARCHAR NOT NULL,
    trade_date         VARCHAR NOT NULL,        -- '*' for non-dated APIs (e.g. stock_basic)
    status             VARCHAR NOT NULL,        -- ok | partial | failed | unauthorized
    row_count          BIGINT,
    cache_class        VARCHAR NOT NULL DEFAULT 'trade_day_immutable',
                                                -- static | trade_day_immutable | trade_day_mutable | hot_or_anns
    ttl_seconds        INTEGER,
    data_completeness  VARCHAR NOT NULL DEFAULT 'final',
                                                -- final | intraday
    synced_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plugin_id, api_name, trade_date)
);

-- Tushare per-call audit (per plugin)
CREATE TABLE IF NOT EXISTS tushare_calls (
    plugin_id   VARCHAR,
    api_name    VARCHAR,
    params_hash VARCHAR,
    rows        INTEGER,
    latency_ms  INTEGER,
    called_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
