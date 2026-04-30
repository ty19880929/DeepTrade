# Changelog

All notable changes to DeepTrade. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and SemVer.

## [v0.5.0] — 2026-04-30 — Plugin CLI dispatch + pure data isolation

Breaking-change reshape per `docs/plugin_cli_dispatch_evaluation.md`. The
project is in dev/iteration phase; **no backward compatibility**.

### Changed (BREAKING)

**Framework command surface — closed**
- Top-level CLI is now `init / config / plugin / data` ONLY. `strategy` and
  `channel` command groups removed.
- Pure pass-through: any unknown first token is looked up as a `plugin_id`;
  if installed + enabled, framework calls `Plugin.dispatch(remaining_argv)`
  and is otherwise dumb.
- `deeptrade --help` no longer enumerates plugin subcommands. `--help`
  inside a plugin is the plugin's own responsibility.
- `deeptrade hello` removed. Interactive main menu removed.
- Reserved plugin_ids: `init`, `config`, `plugin`, `data`.

**Plugin contract — minimal**
- `StrategyPlugin` Protocol removed. New unified `Plugin` Protocol:
  `metadata` + `validate_static(ctx)` + `dispatch(argv) -> int`.
- `ChannelPlugin` extends `Plugin` and adds `push(ctx, payload)`.
- `StrategyContext` / `StrategyParams` / `StrategyRunner` / TUI dashboard
  removed. Each plugin owns its own runtime + run lifecycle internally.
- `ChannelContext` renamed to `PluginContext` (still narrow: db + config +
  plugin_id).

**Data isolation — Plan A (pure)**
- Framework owns ONLY: `app_config`, `secret_store`, `schema_migrations`,
  `plugins`, `plugin_tables`, `plugin_schema_migrations`, `llm_calls`,
  `tushare_sync_state`, `tushare_calls`. No business tables.
- Tushare-derived shared market tables (`stock_basic`, `trade_cal`, `daily`,
  `daily_basic`, `moneyflow`) removed from core. Each strategy plugin
  declares its own prefixed copies (e.g. `lub_stock_basic`, `va_*`).
- `tushare_sync_state` PK now `(plugin_id, api_name, trade_date)`. Each
  plugin tracks its own sync state and cache; no cross-plugin sharing.
- `tushare_calls` and `llm_calls` add `plugin_id` column.
- `TushareClient.__init__` requires `plugin_id`. Framework probes use the
  reserved `FRAMEWORK_PLUGIN_ID = "__framework__"` sentinel.

**Notification API**
- `core/notifier.py` exposes top-level `notify(db, payload)` and
  `notification_session(db)`. Re-exported as `from deeptrade import
  notify, notification_session`. NoopNotifier when no channel enabled.
- `strategy_runs` / `strategy_events` tables removed. Each plugin defines
  its own `<prefix>_runs` / `<prefix>_events` if it wants run history.

### Added

- Built-in plugins reshaped to v0.2.0:
  - `limit-up-board`: own `cli.py` + `plugin.py` + `runner.py` + `runtime.py`;
    new migration with 10 `lub_*` tables.
  - `volume-anomaly`: same pattern; 5 `va_*` tables; subcommands `screen`
    / `analyze` / `prune` / `history` / `report`.
  - `stdout-channel`: implements new `Plugin` + `ChannelPlugin` contracts;
    own `dispatch` for `test` / `log`.
- Tests: framework routing tests (`tests/cli/test_routing.py`); Plugin
  Protocol contract tests (`tests/plugins_api/test_protocol.py`); plugin
  install + migration isolation tests (`tests/core/test_plugin_install.py`).

### Removed

- `cli_strategy.py`, `cli_channel.py`, `tui/` package, `core/strategy_runner.py`,
  `core/context.py`, old plugin `strategy.py` files.
- `textual` dependency.
- `_interactive_main_menu`, `hello` command.

## [v0.1.0] — 2026-04-28

First public release. Baseline implementation of DESIGN.md v0.3.1.

### Added

**Framework**

- `deeptrade init` — DuckDB layout + 11-table core schema migrations (idempotent).
- `deeptrade config show / set / set-tushare / set-deepseek / test` — layered config (env > db > default), keyring + plaintext fallback for secrets.
- `deeptrade plugin install / list / info / disable / enable / upgrade / uninstall [--purge]` — three-stage lifecycle (install **never** touches network; validate is connectivity-only; run does the strict checks).
- `deeptrade strategy list / run / history / report <run_id>` — Live EVA-themed dashboard (header / progress / events / analysis / footer); `--no-dashboard` for non-tty.
- Plugin api_version "1": `StrategyPlugin` Protocol, `StrategyContext`, `StrategyEvent` enum, Pydantic `PluginMetadata` (YAML).

**Core services**

- `Database` — single-process, single-writer DuckDB; reentrant write lock; short transactions.
- `SecretStore` — keyring-first, plaintext fallback with explicit warning.
- `TushareClient` — token-bucket rate limit, tenacity retries, 4 cache classes (static / trade_day_immutable / trade_day_mutable / hot_or_anns), `data_completeness` (final / intraday) for intraday isolation.
- `DeepSeekClient` — JSON-mode + Pydantic double-validate; profile triple (`fast` / `balanced` / `quality`); stage-level `max_output_tokens` (R1/R2 default 32k, final_ranking 8k); **never** passes `tools` / `tool_choice` / `functions`.
- `StrategyRunner` — status state machine (running → success / failed / partial_failed / cancelled); KeyboardInterrupt → cancelled; any `VALIDATION_FAILED` event flips success → partial_failed.
- `setup_logging()` — stderr handler + rotating file under `~/.deeptrade/logs/`.

**Built-in strategy: limit-up-board**

- Step 0 `resolve_trade_date()` — most-recent-closed trade day; `app.close_after` configurable threshold; `--allow-intraday` opt-in.
- Step 1 data assembly — main board filter (Q2: SSE/SZSE only), ST/suspended exclusion, three-tier `sector_strength` fallback (`limit_cpt_list` → `lu_desc_aggregation` → `industry_fallback`), normalized prompt fields (亿/万 + 2dp) while DB keeps raw.
- Step 2 R1 — `plan_r1_batches()` with input + output token DUAL budget (F5); `EvidenceItem.unit` mandatory.
- Step 4 R2 — single batch by default; auto multi-batch when input exceeds budget.
- Step 4.5 `final_ranking` — only triggered on multi-batch R2; `select_finalists()` keeps top + watchlist + boundary avoid samples.
- Step 5 reports — 5-file dump under `~/.deeptrade/reports/<run_id>/`; banner stack rules (red for partial_failed/failed/cancelled, yellow for INTRADAY MODE, both stack).

### Fixed (v0.3 review round 2 → v0.3.1)

- **F1** `limit_step` was duplicated in optional table → removed; required-only.
- **F2** `limit_cpt_list` `✅` mismatched optional status → unified to `optional+fallback` everywhere; `sector_strength_source` label propagated to prompts.
- **F3** Fast profile R2 `thinking: true` contradicted "all off" docs → flipped to `false`.
- **F4** `--allow-intraday` would have polluted EOD caches → added `data_completeness` column; daily-mode reader rejects intraday-cached rows; UI/report INTRADAY MODE banner.
- **F5** Default `max_output_tokens=8192` would truncate R1 → moved to per-stage profile (R1/R2 32k, final_ranking 8k); R1 evidence cap 8 → 4; rationale length-capped via prompt.
- **S1** Migrations are now the **sole** DDL source; `tables` only declares names + purge flag.
- **S2** `app.close_after` configurable (default 18:00); install never touches tushare.
- **S3** `strategy_runs.status` CHECK constraint removed (DuckDB ALTER limitation); validation moved to Pydantic layer.
- **S4** `row_count=0` is a legal outcome (extreme tape day); fallback predicate accepts it.
- **S5** `final_ranking` only ranks finalists; non-finalists keep `batch_local_rank`; both surface in `round2_predictions.json`.

### Engineering

- 163 pytest tests passing; ruff + mypy clean.
- 1 real concurrency bug fixed during development: `Database.transaction()` + `execute()` self-deadlock with non-reentrant `threading.Lock` → switched to `threading.RLock`.
- 1 pandas 2.x compat fix: `pd.read_json(json_str)` deprecated → wrap in `io.StringIO()`.

### Known design debts (planned for v0.4)

- **D1** Replace `configure(ctx) -> dict` with schema-driven `get_param_schema() -> type[BaseModel]`; CLI auto-renders questionary forms; non-interactive mode supports `--params-file`.
- **D2** Per-API `probes` in plugin metadata; `validate` becomes two layers (`validate_connectivity` + `validate_required_apis`).

[v0.1.0]: https://github.com/example/deeptrade/releases/tag/v0.1.0
