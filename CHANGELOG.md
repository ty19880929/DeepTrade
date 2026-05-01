# Changelog

All notable changes to DeepTrade. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and SemVer.

## [v0.7.0] — 2026-05-01 — Stage 概念归插件 + 配置键改名

清理 v0.6 留下的 stage 硬编码技术债。Stage 名字、preset → stage tuning 表全
部移入插件；框架的 `LLMClient.complete_json` 不再认识 stage，由调用方直接传
入 `StageProfile`。配置键 `deepseek.profile` 同步重命名为 `app.profile`。
**Breaking change**（项目仍在 dev/iteration 期）。设计原文：DESIGN.md §10.1。

### Changed (BREAKING)

**框架瘦身 — stage 退出 LLMClient**

- 删除 `core.llm_client.KNOWN_STAGES` / `LLMUnknownStageError` /
  `_stage_profile()` / 全局 `_CURRENT_STAGE`。
- `LLMClient.complete_json` 签名变化：
  - 删除 `stage: str` 入参；framework 不再写 `llm_calls.stage` 列。
  - 新增 `profile: StageProfile`（必填）— 调用方直接传入已解析的调参档。
  - `LLMClient.__init__` 删除 `profiles=` 入参。
  - `LLMManager.get_client()` 不再绑 profile。
- `RecordedTransport` 改为纯 FIFO：`register(response)` 不再带 stage 标签。
- 删除 `core.config.DS_STAGES` / `DeepSeekProfileSet` / `PROFILES_DEFAULT`
  / `ConfigService.get_profile()`。
- `StageProfile` 升格为公共契约，搬到 `deeptrade.plugins_api.llm`，由
  `from deeptrade.plugins_api import StageProfile` 公开导出。

**配置键改名 — `deepseek.profile` → `app.profile`**

- `AppConfig.deepseek_profile` → `AppConfig.app_profile`；`_DOT_TO_FIELD`
  同步更新。preset 仍是 `Literal["fast","balanced","quality"]`，语义全局，
  但键名 vendor-agnostic。
- DB 行自动迁移：`config_migrations.migrate_legacy_deepseek_profile_key`
  幂等地把 `deepseek.profile` 行改写为 `app.profile`，并删除旧行。
- **环境变量直接断代**：`DEEPTRADE_DEEPSEEK_PROFILE` 不再被识别。
  `ConfigService.get_app_config()` 启动时若检测到旧 env 而新 env 未设，
  抛 `RuntimeError` 退出 — 避免静默用错配置（默认会回落到 "balanced"，
  让用户以为生效但其实并没有）。请改为 `DEEPTRADE_APP_PROFILE`。

**DB schema — `llm_calls.stage` 列删除**

- 新 SQL 迁移 `20260501_002_drop_llm_calls_stage.sql` `ALTER TABLE
  llm_calls DROP COLUMN IF EXISTS stage`（DuckDB 1.0+）。
- `core/migrations/core/20260427_001_init.sql` 同步去掉 `stage` 字段，
  让 fresh DB 直接落到 v0.7 期望状态。
- 历史 run 的 stage 信息仍可在
  `~/.deeptrade/reports/<run_id>/llm_calls.jsonl` 中查阅；v0.7 起新写入的
  jsonl 行也不再含 `stage` 键。

**插件改造（两个内建插件）**

- 新增 `<plugin>/profiles.py`：本地维护 preset → stage tuning 表 +
  `resolve_profile(preset, stage)`。
- `runner.py` 读取 `cfg.app_profile`（preset 字符串），传给 pipeline 函数；
  pipeline 内部调 `resolve_profile()` 得到 `StageProfile`。
- `volume-anomaly` 借此改造把语义错误的 stage 名 `continuation_prediction`
  改回 `trend_analysis`。

### Added

- `deeptrade/plugins_api/llm.py` — 公共 `StageProfile` 契约（4 字段：
  thinking / reasoning_effort / temperature / max_output_tokens）。
- `config_migrations.migrate_legacy_deepseek_profile_key` + 4 条单测
  （rename / 幂等 / fresh DB no-op / new-already-set 跳过）。
- `tests/core/test_config.py` 新增两条 env 行为测试（旧 env 报错、新旧并存
  以新为准）。

### Engineering

- 136 pytest tests passing (无变更)。
- 偿还 v0.6 RV6-4 / RV6 §10.2 已知技术债。

## [v0.6.0] — 2026-05-01 — LLM Manager 化（多 Provider）

The LLM client is no longer DeepSeek-specific — it is now a framework-level
service that lets a single plugin call multiple OpenAI-compatible LLMs in
the same run. **Breaking change** (project remains in dev/iteration phase).
Full rationale: `DESIGN.md` §0.7 + §10.

### Changed (BREAKING)

**Configuration model — `deepseek.*` → `llm.*`**

- `llm.providers` (JSON dict, app_config) — `{name: {base_url, model, timeout}}`.
  Multiple providers coexist; each plugin picks by name at call time.
- `llm.<name>.api_key` (secret_store) — one secret per provider. The
  `is_secret_key()` predicate (replacing the old `SECRET_KEYS` constant)
  matches `tushare.token` plus this dynamic prefix.
- `llm.audit_full_payload` (bool, app_config) — replaces
  `deepseek.audit_full_payload`.
- `deepseek.profile` is **kept** as the global stage-profile name (rename
  deferred to v0.7 per §10.1 note).
- The four legacy keys `deepseek.base_url` / `deepseek.model` /
  `deepseek.timeout` / `deepseek.audit_full_payload` are removed from
  `AppConfig`. There is **no `llm.default`** — callers must pass a name.

**Auto-migration**

- On first `apply_core_migrations()` after upgrade, legacy `deepseek.*`
  rows are migrated into `llm.providers["deepseek"]` + the renamed secret
  `llm.deepseek.api_key` + `llm.audit_full_payload`. Idempotent: re-runs
  on already-migrated DBs are no-ops. Code in
  `deeptrade.core.config_migrations.migrate_legacy_deepseek_keys`.

**LLM client / new manager**

- New `core/llm_manager.py::LLMManager` — the only path plugins should use:
  `list_providers()`, `get_provider_info(name)`, `get_client(name, *,
  plugin_id, run_id, reports_dir=None)`. Caches clients per
  `(name, plugin_id, run_id)`. Documented as not thread-safe.
- `core/deepseek_client.py` → `core/llm_client.py`;
  `DeepSeekClient` → `LLMClient`. `OpenAIClientTransport` keeps its name
  (it really is the OpenAI-compatible transport).
- `LLMNotConfiguredError` (new) — raised by manager when a provider is
  missing or its api_key is unset.

**CLI command surface**

- Added: `config set-llm` (interactive new / edit / delete one provider),
  `config list-llm` (show usable providers), `config test-llm [name]`
  (per-provider connectivity check; tests all when omitted).
- Removed: `config set-deepseek`, `config test`. The init-time prompt
  ("Configure deepseek now?") is now "Configure an LLM provider now?".
- `config show` expands `llm.providers` so each provider's `api_key` slot
  is its own masked row.

**Plugin runtime collapse**

- `volume-anomaly` and `limit-up-board` runtimes lose their per-plugin
  `build_llm_client(rt)`; both now declare `llms: LLMManager` and call
  `rt.llms.get_client(provider_name, plugin_id=, run_id=, reports_dir=)`.
  Provider selection helper `pick_llm_provider(rt)` ships in each
  plugin's `runtime.py` (prefers `deepseek`, falls back to first
  available); a per-plugin `default_llm` config key is deferred to v0.7.

### Added

- `LLMProviderConfig` Pydantic model (per-provider connection record).
- `ConfigService.set_llm_provider(name, *, base_url, model, timeout,
  api_key=None)` and `delete_llm_provider(name)` — CRUD helpers used by
  the CLI.
- `tests/core/test_llm_manager.py` — list/info/get_client + cache + missing
  api_key/provider errors + multi-provider coexistence (11 cases).
- `tests/core/test_config_migrations.py` — idempotency + happy path +
  partial-legacy-state edge case (7 cases).

### Engineering

- 136 pytest tests passing (was 130; +11 manager + 7 migration − 12 retired
  duplicates).
- ruff + mypy clean on touched files.
- DESIGN §0.7 + §7.1 + §7.3 + §10 rewritten; PLAN §11 adds the v0.6 work
  breakdown.

### Known design debts (deferred)

- `KNOWN_STAGES` (`strong_target_analysis / continuation_prediction /
  final_ranking`) is still hardcoded in the framework — leaks plugin
  semantics into `core/llm_client.py`. v0.7 will let plugins declare their
  own stage names + per-stage profile overrides.
- `deepseek.profile` key name retained for backward compat within the
  current dev cycle; rename to `llm.profile` planned for v0.7.
- No transport plugin type yet — Anthropic native / Gemini native cannot be
  added without code changes. Will land as `type=llm-transport` plugins
  when first user need surfaces.

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
