# DeepTrade 开发迭代计划

> 配套文档：`DESIGN.md` v0.3.1
> 计划版本：v1.0 · 2026-04-27
> 适用范围：MVP（v0.1 release）

---

## 1. 总览

### 1.1 目标

把 `DESIGN.md` 的所有"必须"约束（M1-M5、F1-F5、S1-S5）正确、完整地落地为可运行 + 可测的代码，发布 v0.1 release。

### 1.2 时间盒

合计 **17.5 人日**（单人，每日 4-6 工时实算）。比 v0.3 §15 的 14.5 人日多 3 人日，差额来源：

- V0.0 骨架（半天，原计划缺）
- M5 实事估算（5 → 5.0，仅细化分拆但维持总量）
- V0.8 容错收尾、V0.9 文档样例工作量调实
- 1.5 人日联调缓冲

### 1.3 与 DESIGN.md 的关系

DESIGN.md 是"做什么 / 为什么这样做"的**规格**；PLAN.md 是"按什么顺序 / 用什么手段做"的**执行**。计划层面的变更（任务拆分、估算调整、ADR 增补）在本文件维护；设计层面的变更（约束、接口、数据流）继续在 DESIGN.md 维护。

### 1.4 迭代命名约定

`V0.x` 表示一个完整的开发迭代单元，对应 1 个 PR / 一组紧密提交。每个 V0.x **必须**：

- 自带可运行的 demo 命令（即使是 fake / placeholder）
- 通过自身的 DoD pytest 用例
- pre-commit 通过
- main 分支保持随时可发布的"绿色"状态

### 1.5 工作日定义

- 1 人日 = 4-6 小时实算编码 + 1-2 小时本地验证
- 估算不含：阅读 DESIGN.md、设计 review、外部沟通、上下文切换
- 估算精度：±30%；超过 ±50% 必须反馈调计划

---

## 2. 全局约定

### 2.1 项目骨架

```
deeptrade/                              # 项目根
├── pyproject.toml                      # uv 管理
├── uv.lock
├── ruff.toml
├── mypy.ini
├── pytest.ini
├── .pre-commit-config.yaml
├── .gitignore
├── README.md
├── DESIGN.md
├── PLAN.md                             # 本文件
├── deeptrade/                          # 主包
│   ├── __init__.py
│   ├── cli.py
│   ├── theme.py
│   ├── core/
│   │   ├── paths.py / db.py / secrets.py / config.py
│   │   ├── tushare_client.py / deepseek_client.py
│   │   ├── plugin_manager.py / strategy_runner.py / context.py
│   │   └── migrations/core/<version>.sql
│   ├── plugins_api/
│   │   ├── base.py / metadata.py / events.py
│   ├── strategies_builtin/
│   │   └── limit_up_board/
│   │       ├── deeptrade_plugin.yaml
│   │       ├── migrations/<version>.sql
│   │       ├── strategy.py / data.py / prompts.py
│   │       ├── schemas.py / pipeline.py / render.py
│   └── tui/
│       ├── welcome.py / question_box.py
│       ├── dashboard.py / widgets.py
├── tests/
│   ├── conftest.py
│   ├── core/
│   ├── cli/
│   ├── tui/
│   ├── strategies/lub/
│   ├── _fake_strategy/                 # 测试用最小插件
│   └── fixtures/
│       ├── tushare/<api>_<scenario>.json
│       └── llm/<stage>_<scenario>.json
└── scripts/
    ├── record_tushare_fixture.py
    └── record_llm_fixture.py
```

### 2.2 工具链版本

| 工具 | 版本 | 用途 |
|---|---|---|
| Python | 3.11+ | — |
| uv | latest | 包管理 / 锁文件 / 虚拟环境 |
| ruff | ≥ 0.5 | format + lint |
| mypy | ≥ 1.10 | 类型检查（non-strict + Pydantic v2 plugin） |
| pytest | ≥ 8.0 | 测试 |
| pre-commit | ≥ 3.7 | 钩子 |

### 2.3 提交节奏

- 每个 V0.x **一个 PR**（也允许"功能 PR + 紧随测试 PR"两段提交）
- 每个 PR 必须含：feature commit(s) + test commit(s) + 文档增量（如有）
- main 永远绿色：合并前 pre-commit + pytest 全过

### 2.4 分支策略

```
main                    # 永远绿色，对应"已发布"
└─ feat/v0.0-skeleton  # 一个迭代一个分支
└─ feat/v0.1-db
└─ ...
```

无 release 分支（v0.1 是首发）。tag 在 V0.9 完成后打 `v0.1.0`。

### 2.5 验证方式（每个 V0.x 完成时）

1. **机器验证**：`uv run pytest -q` + `uv run pre-commit run --all-files` 全过
2. **人工冒烟**：执行该 V0.x 的"Commands that work"段中的命令，输出符合预期
3. **回归**：上一 V0.x 的 demo 命令仍然能跑通（main 绿色）

---

## 3. 迭代 DAG

```
                     V0.0 (骨架)
                          │
                          ▼
                     V0.1 (DuckDB + secret_store)
                          │
                          ▼
                     V0.2 (Config CRUD)
                       ┌──┴──────────────┐
                       ▼                 ▼
                  V0.3 (TushareClient)   V0.4 (DeepSeekClient)
                       └──┬──────────────┘
                          ▼
                     V0.5 (Plugin Manager + fake_strategy)
                          │
                          ▼
                     V0.6 (Dashboard + 事件流)
                          │
                          ▼
                  V0.7a (LUB 数据层)
                          │
                          ▼
                  V0.7b (LUB LLM 层)
                          │
                          ▼
                  V0.7c (LUB 渲染与报告)
                          │
                          ▼
                     V0.8 (容错与日志收尾)
                          │
                          ▼
                     V0.9 (文档与样例)  ──→  v0.1.0 tag
```

**关键并行点**：V0.3 与 V0.4 互不依赖，可并行（单人开发也建议交替推进，避免长时间停留在同一类问题上）。

---

## 4. 各迭代详细规格

### V0.0 — 项目骨架（0.5 人日）

**Goal**：让 `deeptrade --version` 与 `deeptrade hello` 能跑，奠定测试与质检基线。

**Deliverables**：

- `pyproject.toml`（依赖见 DESIGN §3，Python ≥ 3.11，包名 `deeptrade`，入口脚本 `deeptrade = "deeptrade.cli:app"`）
- `uv.lock`
- `ruff.toml`（line-length 100, target py311, 主要规则集 E/F/I/UP/B）
- `mypy.ini`（non-strict, `plugins = pydantic.mypy`, `ignore_missing_imports = True`）
- `pytest.ini`（含 `markers = manual: 需要人工验证`）
- `.pre-commit-config.yaml`（ruff format + ruff check + mypy + 不调用网络）
- `deeptrade/__init__.py`（`__version__ = "0.0.1"`）
- `deeptrade/cli.py`（typer App + `version` callback + `hello` 命令）
- `deeptrade/theme.py`（占位：仅 EVA tokens 定义 + EVA_THEME，不应用）
- `tests/test_smoke.py`（一个测 `--version` 输出包含 `0.0.1`）
- `README.md`（占位：项目介绍 + uv 安装命令）
- `.gitignore`（标准 Python + .venv + .deeptrade）

**Commands that work**：

```bash
$ uv sync
$ uv run deeptrade --version
DeepTrade 0.0.1
$ uv run deeptrade hello
[NERV ▌ DEEPTRADE] Hello.
$ uv run pytest -q
1 passed
$ uv run pre-commit run --all-files
[OK]
```

**DoD pytest cases**：
1. `test_version_output` — `--version` 输出含 `0.0.1`
2. `test_hello_runs` — `hello` 退出码 0 且 stdout 非空

**Depends on**：无（首个迭代）

**Risks**：

- uv 在用户环境未安装 → README 提供 `pip install -e .` 兜底说明
- Windows PowerShell 5.1 ANSI 渲染弱 → `theme.py` 此版本不强制启用，`hello` 用纯 ASCII

---

### V0.1 — DuckDB + secret_store + core migrations（1 人日）

**Goal**：数据库层就位；`deeptrade init` 能完整建表，幂等。

**Deliverables**：

- `deeptrade/core/paths.py`：解析 `~/.deeptrade/`（含 logs/ reports/ plugins/）
- `deeptrade/core/db.py`：
  - `Database` 类管理单写连接
  - `apply_core_migrations()` 按 `core/migrations/` 顺序执行 + 写 `schema_migrations`
  - 短事务上下文管理器
- `deeptrade/core/secrets.py`：
  - `SecretStore` 接口
  - `KeyringBackend`（首选）+ `PlaintextBackend`（降级）
  - 自动探测 keyring 可用性
- `deeptrade/core/migrations/core/20260427_001_init.sql`：DESIGN §6.1 + §6.2 全部 11 张表
- `deeptrade/cli.py` 加 `init` 命令（先 stub，仅建库 + 应用 migrations，token 收集留待 V0.2）
- 单元测试

**Commands that work**：

```bash
$ uv run deeptrade init
✔ Database created: ~/.deeptrade/deeptrade.duckdb
✔ Schema applied: 20260427_001_init
$ uv run deeptrade init     # 幂等
✔ Database already initialized; schema up-to-date
```

**DoD pytest cases**（关键 8 项）：
1. `test_init_creates_db_file_and_dirs`
2. `test_init_is_idempotent`
3. `test_apply_core_migrations_records_version`
4. `test_apply_core_migrations_skips_applied_versions`
5. `test_secret_store_keyring_roundtrip`
6. `test_secret_store_falls_back_to_plaintext_when_keyring_unavailable`（用 monkeypatch 模拟 keyring 失败）
7. `test_strategy_runs_status_validated_by_pydantic_not_check`（验证 S3：DDL 无 CHECK，Pydantic 兜底）
8. `test_tushare_sync_state_has_data_completeness_column`（验证 F4 列存在）

**Depends on**：V0.0

**Risks**：

- DuckDB 1.x 在 Windows 上写文件偶发权限问题 → 用 `tmp_path` 测试 + 文档提示用户避免 OneDrive 同步路径

---

### V0.2 — Config CRUD + 配置命令（1 人日）

**Goal**：配置可读写；`deeptrade config show/set/test` 全部工作。

**Deliverables**：

- `deeptrade/core/config.py`：
  - `AppConfig` Pydantic 模型（含 §7.1 全部字段；`app.close_after` 默认 `time(18, 0)`）
  - `ConfigService.get(key)`：优先级 env > secret_store > app_config > default
  - `ConfigService.set(key, value, *, is_secret=False)` 自动路由到正确表
  - 密钥 `tushare.token` / `deepseek.api_key` 强制走 secret_store
- `deeptrade/cli.py` 加 `config` 子命令组：
  - `config show`：表格输出（密钥脱敏，仅显示后 4 位）
  - `config set tushare`：交互式（token + rps + timeout）
  - `config set deepseek`：交互式（key + base_url + model + profile）
  - `config set <key> <value>`：脚本化场景
  - `config test`：tushare `stock_basic(limit=1)` + DeepSeek 1-token echo
- `deeptrade/cli.py` 增强 `init` 命令：建库后顺序进入 `config set tushare` 与 `config set deepseek`（可跳过）

**Commands that work**：

```bash
$ uv run deeptrade init
... (既有) ...
? Configure tushare now? Y
? Tushare token: ********
✔ Saved
? Configure deepseek now? Y
? DeepSeek API key: ********
? Profile [balanced]:
✔ Saved
$ uv run deeptrade config show
+-----------------------+----------------+----------------+
| Key                   | Value          | Source         |
+-----------------------+----------------+----------------+
| app.timezone          | Asia/Shanghai  | default        |
| app.close_after       | 18:00          | default        |
| tushare.token         | ********kd9    | secret_store   |
| deepseek.profile      | balanced       | app_config     |
| ...
$ uv run deeptrade config test
✔ Tushare connectivity ok (stock_basic limit=1 → 5247 rows, 320ms)
✔ DeepSeek echo ok (1.2s)
```

**DoD pytest cases**：
1. `test_config_show_masks_secrets`
2. `test_env_overrides_db_and_default`（如 `DEEPTRADE_DEEPSEEK_PROFILE=fast`）
3. `test_config_set_secret_routes_to_secret_store`
4. `test_config_set_non_secret_routes_to_app_config`
5. `test_config_test_handles_invalid_tushare_token`（fixture transport 返回 401）
6. `test_close_after_default_is_18_00`（验证 S2）
7. `test_close_after_can_be_overridden`（如 `app.close_after = 17:30`）

**Depends on**：V0.1

**Risks**：

- `config test` 在 V0.2 阶段还没有真实的 TushareClient/DeepSeekClient → 暂用占位实现（直接调 SDK，简单异常捕获），V0.3/V0.4 完成后回填

---

### V0.3 — TushareClient + fixtures（1.5 人日）

**Goal**：Tushare 调用层完整就位；测试不烧积分；4 类缓存 + 盘中隔离 + 故障注入全部覆盖。

**Deliverables**：

- `deeptrade/core/tushare_client.py`：
  - `TushareTransport` 抽象（生产 `TushareSDKTransport`，测试 `FixtureTransport`）
  - `TushareClient` 类
    - 令牌桶限流（`rps` 可配置 + 429 自适应减半）
    - tenacity 重试（指数退避，max=5）
    - 4 类缓存策略（DESIGN §11.1）
    - `data_completeness` 写入逻辑（盘中模式自动标 intraday）
    - `tushare_sync_state` 维护
    - `tushare_calls` 审计
    - `can_fallback()` 函数（DESIGN §13.2，含盘中拒绝）
- `tests/fixtures/tushare/`：
  - `stock_basic.json`、`trade_cal_2026Q2.json`
  - `limit_list_d_20260427.json`（典型 70+ 涨停）
  - `limit_list_d_20260205_zero.json`（极端 0 涨停日）
  - `limit_step_20260427.json`、`daily_basic_20260427_300.json`
  - `_faults/{429,5xx,unauthorized,timeout}.json`
- `scripts/record_tushare_fixture.py`：录制脚本（接 token / api / params，写入 fixture）
- `tests/core/test_tushare_client.py`

**Commands that work**：

```bash
# 用户层暂无新命令；config test 复用此客户端
$ uv run pytest tests/core/test_tushare_client.py -v
14 passed
```

**DoD pytest cases**（14 项，覆盖所有关键路径）：
1. `test_call_uses_fixture_transport_when_injected`
2. `test_cache_hit_skips_api_call`
3. `test_cache_class_static_respects_7day_ttl`
4. `test_cache_class_immutable_no_refetch_after_ok`
5. `test_cache_class_mutable_refetches_on_T_or_T_plus_1`
6. `test_cache_class_hot_or_anns_respects_6h_ttl`
7. `test_intraday_run_writes_data_completeness_intraday`（**F4 关键**）
8. `test_eod_run_rejects_intraday_cache_and_refetches`（**F4 关键**）
9. `test_intraday_run_can_use_intraday_cache`
10. `test_unauthorized_marks_state_does_not_raise`
11. `test_429_triggers_rps_decay_and_persists`
12. `test_5xx_retries_then_falls_back_when_can_fallback`
13. `test_5xx_no_fallback_when_intraday_cache_in_eod_run`
14. `test_can_fallback_accepts_row_count_zero_when_status_ok`（**S4 关键**）

**Depends on**：V0.1

**Risks**：

- 录制 fixture 需要真实 token，且 tushare 接口字段会随时间变更 → fixture 文件头注释录制日期；CI 仅依赖已录制 fixture，不联网

---

### V0.4 — DeepSeekClient + recorded fixtures（1.5 人日）

**Goal**：LLM 调用层就位；JSON 强约束 + 双重校验 + recorded transport；profile 三档与 stage 级 max_output_tokens 全覆盖。

**Deliverables**：

- `deeptrade/core/deepseek_client.py`（DESIGN §10.2 完整实现）：
  - `LLMTransport` 抽象（生产 `OpenAIClientTransport`，测试 `RecordedTransport`）
  - profile 三档配置加载（`fast/balanced/quality`）
  - `complete_json(system, user, schema, *, stage)` 入口
  - stage 级 max_output_tokens 取参
  - thinking + reasoning_effort 按 stage 取参
  - **永不传** tools / tool_choice / functions（断言 + 测试）
  - tenacity 重试（json/pydantic 失败 1 次重试）
  - `llm_calls` 流水落库（含 prompt_hash）
- `tests/fixtures/llm/`：
  - `strong_target_analysis_happy_batch1.json`
  - `strong_target_analysis_set_mismatch.json`（模拟集合不一致）
  - `continuation_prediction_happy_single_batch.json`
  - `continuation_prediction_happy_multi_batch_b1.json` / `_b2.json`
  - `final_ranking_happy.json`
  - `_faults/{json_invalid,timeout,5xx}.json`
- `scripts/record_llm_fixture.py`
- `tests/core/test_deepseek_client.py`

**DoD pytest cases**（11 项）：
1. `test_complete_json_validates_with_pydantic`
2. `test_complete_json_retries_once_on_json_decode_error`
3. `test_complete_json_retries_once_on_pydantic_validation_error`
4. `test_complete_json_uses_stage_specific_max_output_tokens`（**F5 关键**：R1 32k vs final_ranking 8k）
5. `test_fast_profile_has_thinking_disabled_for_all_stages`（**F3 关键**）
6. `test_balanced_profile_disables_thinking_for_r1_only`
7. `test_quality_profile_enables_thinking_for_all`
8. `test_no_tools_param_passed_in_chat_create`（**M3 关键**：mock OpenAI 客户端，断言无 `tools` 入参）
9. `test_complete_json_persists_llm_calls_record_with_prompt_hash`
10. `test_recorded_transport_replays_response_deterministically`
11. `test_unknown_stage_raises_assertion`

**Depends on**：V0.1（与 V0.3 并行）

**Risks**：

- DeepSeek V4 SDK 与 OpenAI SDK 兼容点（如 reasoning_effort 字段位置）需要实跑确认 → 录制 fixture 时同步验证

---

### V0.5 — Plugin Manager + fake_strategy（2 人日）

**Goal**：插件系统能装能卸能跑；用 fake_strategy 自测，不依赖 limit-up-board。**这是 bootstrap 关键迭代**。

**Deliverables**：

- `deeptrade/plugins_api/metadata.py`：DESIGN §8.2 全部 Pydantic 模型
- `deeptrade/plugins_api/base.py`：`StrategyPlugin` Protocol（含 `validate_static`）
- `deeptrade/plugins_api/events.py`：DESIGN §8.5 EventType 枚举 + `StrategyEvent` 模型
- `deeptrade/core/context.py`：`StrategyContext`（暴露 `tushare` / `llm.complete_json` / `db` / `emit`；**不**暴露任何 tool 接口）
- `deeptrade/core/plugin_manager.py`：
  - `install(path)`：DESIGN §8.3 完整流程
  - `validate(plugin_id)`：仅连通性自检（D2 简化版）
  - `list()` / `info(plugin_id)` / `disable` / `enable` / `uninstall(--purge)` / `upgrade(path)`
  - migrations 唯一执行源 + checksum 校验
  - plugin_tables 登记
  - 拒绝 `permissions.llm_tools=true`（M3）
- `deeptrade/core/strategy_runner.py`：
  - 调度 plugin.run()
  - 事件持久化到 strategy_events
  - status 状态机（running → success/failed/partial_failed/cancelled）
  - Ctrl+C 捕获 → cancelled
- `deeptrade/cli.py` 加 `plugin` 与 `strategy` 子命令组
- `tests/_fake_strategy/`：
  - `deeptrade_plugin.yaml`（含 1 张表 + 1 个 migration，无 tushare / llm 依赖）
  - `migrations/20260427_001_init.sql`
  - `fake_strategy/strategy.py`：5 个 yield 事件 + 1 个 mock LLM 调用（用 RecordedTransport）

**Commands that work**：

```bash
$ uv run deeptrade plugin install ./tests/_fake_strategy
─── 即将安装 ────...
确认? y
✔ 已安装

$ uv run deeptrade plugin list
+----------------+---------+---------+----------------+
| plugin_id      | version | enabled | validation     |
+----------------+---------+---------+----------------+
| fake-strategy  | 0.1.0   | yes     | not_validated  |

$ uv run deeptrade strategy run fake-strategy
... 事件流输出（无 dashboard，先纯文本） ...
✔ status: success

$ uv run deeptrade plugin uninstall fake-strategy --purge
确认删除 fake-strategy 的 1 张表? y
✔ 已卸载并清理
```

**DoD pytest cases**（13 项）：
1. `test_install_parses_yaml_and_runs_migrations_in_transaction`
2. `test_install_rejects_when_llm_tools_true`（**M3 关键**）
3. `test_install_does_not_call_tushare_or_llm`（**S2 关键**：mock transports，断言 0 调用）
4. `test_install_validates_migration_checksum`（篡改文件应失败）
5. `test_install_rolls_back_on_ddl_failure`
6. `test_install_rejects_duplicate_plugin_id`
7. `test_uninstall_default_keeps_tables_and_records`
8. `test_uninstall_purge_drops_tables_and_clears_records`
9. `test_upgrade_skips_already_applied_migrations`
10. `test_upgrade_executes_only_new_migrations`
11. `test_run_records_strategy_events_in_db`
12. `test_run_partial_failed_when_event_stream_emits_validation_failed`（**M5 关键**）
13. `test_context_does_not_expose_tool_call_method`（**M3 关键**：用 `dir(ctx.llm)` 断言无 `chat_with_tools` 等）

**Depends on**：V0.1, V0.2, V0.3, V0.4

**Risks**：

- 鸡生蛋（已通过 fake_strategy 解决）
- entry-point 加载导致 sys.path 污染 → 用 `importlib.util.spec_from_file_location` 隔离加载

---

### V0.6 — Dashboard + 事件流（1.5 人日）

**Goal**：Live Layout 跑起来；事件流实时驱动；EVA 主题应用；横幅规则就位。

**Deliverables**：

- `deeptrade/theme.py`：DESIGN §9.2 完整 EVA tokens（v0.0 仅占位，本迭代正式启用）
- `deeptrade/tui/welcome.py`：ASCII art + welcome 屏（`get_user_selections` 风格的 Panel）
- `deeptrade/tui/question_box.py`：`create_question_box`
- `deeptrade/tui/widgets.py`：`progress_table()` / `events_table()` / `analysis_panel()` / `footer_stats()`
- `deeptrade/tui/dashboard.py`：
  - `Dashboard` 类（封装 rich.Live + Layout）
  - 消费 strategy_events 流，更新各 panel
  - 横幅渲染规则：
    - `partial_failed` / `failed` / `cancelled` → 红色横幅
    - `is_intraday=True` → 黄色 `INTRADAY MODE` 横幅（**F4 关键**）
    - 两者可叠加
- `deeptrade/core/strategy_runner.py` 增强：可选注入 dashboard 渲染器（不传则纯日志）
- `tests/tui/test_dashboard_render.py`：snapshot 测试（用 `Console.capture()` 抓输出）
- `tests/tui/test_event_stream.py`：纯逻辑测试（事件流 → state diff）

**Commands that work**：

```bash
$ uv run deeptrade strategy run fake-strategy
[完整 Live 看板渲染：Header / Progress / Events / Analysis / Footer]
... 黄字 INTRADAY MODE 横幅（如果 fake_strategy 标 is_intraday=True） ...
✔ status: success
```

**DoD pytest cases**（8 项 + 4 项 manual）：
1. `test_layout_renders_all_panels`
2. `test_partial_failed_renders_red_banner`（**M5 关键**）
3. `test_intraday_mode_renders_yellow_banner`（**F4 关键**）
4. `test_partial_failed_and_intraday_can_stack_two_banners`
5. `test_event_step_progress_updates_progress_panel`
6. `test_event_llm_call_updates_footer_stats`
7. `test_event_log_appends_to_events_panel`
8. `test_dashboard_does_not_crash_on_unknown_event_type`

Manual（标 `@pytest.mark.manual`）：
- 实跑 fake-strategy，看 Layout 在 Windows Terminal / cmd / PowerShell 渲染正确
- EVA 主题色在浅色/深色背景下可读
- questionary 与 Live 切换无 stdout 冲突
- Ctrl+C 后看板能优雅退出

**Depends on**：V0.5

**Risks**：

- Rich Live 与 questionary 同时使用需要互斥（Live 必须 `__exit__` 后才能 questionary）→ 在 `Dashboard` 类用 contextmanager 强约束
- Windows Terminal 与传统 cmd 渲染差异 → `Console(force_terminal=True, color_system="truecolor")`，cmd 上自动 fallback

---

### V0.7a — limit-up-board 数据层（2.5 人日）

**Goal**：数据装配 + fallback + 同步状态全部跑通；输出 `(candidates, market_summary, sector_strength_*, data_unavailable)`。

**Deliverables**：

- `deeptrade/strategies_builtin/limit_up_board/deeptrade_plugin.yaml`：DESIGN §12.2 元数据（含正确的 required/optional 列表）
- `deeptrade/strategies_builtin/limit_up_board/migrations/20260427_001_init.sql`：lub_* 三张表 DDL
- `deeptrade/strategies_builtin/limit_up_board/__init__.py`
- `deeptrade/strategies_builtin/limit_up_board/strategy.py`：骨架（只实现 `validate_static` + `validate` + `sync_data`，run/render 留 stub yield 占位事件）
- `deeptrade/strategies_builtin/limit_up_board/data.py`：
  - `resolve_trade_date()`：DESIGN §12.2 算法（含 `close_after` 配置）
  - `step1_collect_round1()`：DESIGN §12.7 完整实现
  - 主板池过滤（market='主板' AND exchange in ('SSE','SZSE')）
  - ST / 停牌排除
  - sector_strength 三档 fallback（limit_cpt_list → lu_desc 聚合 → industry 聚合）
  - normalized 字段生成（亿/万 + 2 位小数）
  - data_unavailable 列表构建
- 完整 fixture 集（典型日 + 0 涨停日 + 4 种 unauth 组合）
- `tests/strategies/lub/test_data_assembly.py`

**DoD pytest cases**（14 项）：
1. `test_resolve_trade_date_uses_pretrade_when_intraday_no_flag`（**F4 关键**）
2. `test_resolve_trade_date_uses_today_when_after_close_after`
3. `test_resolve_trade_date_with_allow_intraday_uses_today_during_market`
4. `test_resolve_trade_date_respects_app_close_after_config`（**S2 关键**）
5. `test_main_board_filter_excludes_chinext_star_bse`（**Q2 关键**）
6. `test_st_stocks_excluded`
7. `test_suspended_stocks_excluded`
8. `test_zero_candidates_returns_empty_result_not_failure`（**S4 关键**）
9. `test_sector_strength_uses_limit_cpt_list_when_authorized`
10. `test_sector_strength_falls_back_to_lu_desc_aggregation_when_cpt_unauth`（**F2 关键**）
11. `test_sector_strength_falls_back_to_industry_when_both_unauth`
12. `test_normalized_fields_use_yi_wan_units_with_2dp`（**C5 关键**）
13. `test_db_keeps_raw_units_unchanged`
14. `test_data_unavailable_correctly_lists_unauth_apis`

**Depends on**：V0.5, V0.6

**Risks**：

- tushare 字段在录制日期与官方文档间偶有差异（如多/少字段）→ `data.py` 用 `df.get(col, default)` 容错
- fixture 录制成本高 → 优先录 1-2 个典型日，故障场景用 fixture 衍生（手工编辑 row_count 等）

---

### V0.7b — limit-up-board LLM 层（1.5 人日）

**Goal**：双轮 LLM + final_ranking + 集合一致性 + 全部 schema；R1/R2 都能完整跑通。

**Deliverables**：

- `deeptrade/strategies_builtin/limit_up_board/schemas.py`：DESIGN §12.4.2 + §12.5.3 + §12.5.4 全部 Pydantic（含 EvidenceItem.unit、extra=forbid、长度上限）
- `deeptrade/strategies_builtin/limit_up_board/prompts.py`：
  - R1 system + user 模板（含 `sector_strength_source` 占位）
  - R2 system + user 模板
  - final_ranking system + user 模板（仅传 finalists 摘要）
- `deeptrade/strategies_builtin/limit_up_board/pipeline.py`：
  - `run_round1()`：input/output 双 token 预算切批 + 调用 + 集合一致性校验 + 重试 + 单批失败标记
  - `run_round2()`：默认单批；超 token 预算时启用分批
  - `run_final_ranking()`：仅在 R2 多批时触发；finalists 取样
  - 全程 partial_failed 状态机驱动
- `deeptrade/strategies_builtin/limit_up_board/strategy.py`：完成 `run()` 方法
- LLM recorded fixtures（每个 stage 至少 happy + failure 各一组）
- `tests/strategies/lub/test_pipeline.py`

**DoD pytest cases**（14 项）：
1. `test_r1_batch_size_respects_input_budget`
2. `test_r1_batch_size_respects_output_budget`（**F5 关键**）
3. `test_r1_batch_size_takes_min_of_input_and_output`
4. `test_r1_evidence_max_length_4`（**F5 关键**：Pydantic 拒绝 5+）
5. `test_r1_candidate_id_set_equality_check_passes_on_match`
6. `test_r1_set_mismatch_triggers_one_retry_then_marks_batch_failed`
7. `test_r1_partial_failed_propagates_to_run_status`
8. `test_r2_single_batch_skips_final_ranking`
9. `test_r2_multi_batch_triggers_final_ranking`（**M4 关键**）
10. `test_final_ranking_uses_finalists_only_not_all_r2_candidates`（**S5 关键**）
11. `test_final_ranking_uses_8k_max_output_tokens`（**F5 关键**）
12. `test_no_tools_param_in_any_llm_call_in_pipeline`（**M3 关键**：spy 所有 LLM 调用）
13. `test_prompt_contains_sector_strength_source_label`（**F2 关键**）
14. `test_prompt_contains_data_unavailable_list_when_optional_unauth`

**Depends on**：V0.7a

**Risks**：

- LLM 在不同温度下输出的 evidence 长度有方差 → schema 用 max_length=4 强约束 + recorded fixture 确保单元测试稳定

---

### V0.7c — limit-up-board 渲染与报告（1 人日）

**Goal**：`render_result` + reports/ 目录导出 + 重看命令；横幅规则全部就位。

**Deliverables**：

- `deeptrade/strategies_builtin/limit_up_board/render.py`：
  - R1 完成后渲染（Markdown table + analysis panel 更新）
  - R2 完成后渲染（Top 候选 table + 市场温度 + 主线）
  - final_ranking 后渲染（更新 Top 列表用 final_rank）
- 报告导出器（按 DESIGN §12.8.3）：
  - `summary.md`：含红/黄横幅
  - `round1_strong_targets.json`
  - `round2_predictions.json`（含全量，finalists 与 non-finalists）
  - `round2_final_ranking.json`（仅多批时）
  - `llm_calls.jsonl`
  - `data_snapshot.json`
- `deeptrade/cli.py` 加 `strategy report <run_id>` 命令（重看 dashboard + 打开 reports/）
- `tests/strategies/lub/test_render.py`

**Commands that work**：

```bash
$ uv run deeptrade strategy run limit-up-board
... → status: success
$ ls ~/.deeptrade/reports/<run_id>/
data_snapshot.json  llm_calls.jsonl  round1_strong_targets.json
round2_predictions.json  summary.md
$ uv run deeptrade strategy report <run_id>
[Live Analysis Panel 复显 + reports/ 路径]
```

**DoD pytest cases**（8 项）：
1. `test_summary_md_contains_all_required_sections`
2. `test_summary_md_red_banner_on_partial_failed`
3. `test_summary_md_yellow_intraday_banner`（**F4 关键**）
4. `test_summary_md_can_stack_red_and_yellow_banners`
5. `test_round2_predictions_json_includes_non_finalists_with_batch_local_rank`（**S5 关键**）
6. `test_round2_final_ranking_json_only_emitted_when_multi_batch`
7. `test_strategy_report_replays_dashboard`
8. `test_data_snapshot_includes_market_summary_and_candidates_input`

**Depends on**：V0.7b

**Risks**：

- Markdown 在 Rich 渲染嵌套表格时偶发对齐错位 → 报告 md 用纯文本表格 + Rich 转 markdown.Markdown，用 manual 验证

---

### V0.8 — 容错与日志收尾（1 人日）

**Goal**：DESIGN §13 行为矩阵全部覆盖；故障注入 fixture 跑通；history 命令可用。

**Deliverables**：

- 故障注入 fixture 完善（覆盖 §13.1 全部 13 个场景）
- `deeptrade/cli.py` 加 `strategy history` 命令（最近 N 条 run 列表）
- 日志滚动配置（按大小或日期）
- 整合测试：每个 §13.1 场景一个 e2e 用例（用 fake_strategy + 注入 fault transport）

**DoD pytest cases**（约 14 项 e2e）：
13 个 §13.1 行为矩阵场景各一个测试 + `test_history_lists_recent_runs` + `test_logs_rotate_at_size_limit`。

**Depends on**：V0.7c

**Risks**：

- 整合测试数据库状态泄漏 → 用 `tmp_path` fixture 隔离 + 每个测试新建 `Database` 实例

---

### V0.9 — 文档与样例（1 人日）

**Goal**：v0.1.0 release 准备就绪。

**Deliverables**：

- `README.md`（完整：项目介绍 / 安装 / 快速开始 / 子命令清单 / 链接 DESIGN.md & PLAN.md）
- `docs/quick-start.md`：5 分钟跑通 limit-up-board
- `docs/plugin-development.md`：从零写一个新策略插件
- `docs/limit-up-board.md`：策略说明（流程、字段、prompt 节选、报告解读）
- 用法 GIF（asciinema 录制 → svg-term 转换）
- CHANGELOG.md（v0.1.0 条目）
- 打 `v0.1.0` git tag

**DoD**：

- 用户从 README 出发，30 分钟内能完成 install + init + 跑通一次 limit-up-board
- `docs/plugin-development.md` 30 分钟内能完成"从模板到装上"

**Depends on**：V0.8

---

## 5. ADR 决策记录

> 决策记录（Architectural Decision Records），用于回溯实操选择。

### ADR-001：包管理用 uv
- **Decision**：uv，README 中提供 pip + venv 兜底说明
- **Rationale**：与 TradingAgents 一致；锁文件稳定；速度快
- **Alternatives**：pip+venv、poetry、hatch
- **Consequences**：用户需要装 uv（脚本一行）；CI 略有锁文件维护成本

### ADR-002：静态检查 ruff + mypy non-strict
- **Decision**：ruff（format + lint）+ mypy non-strict + Pydantic v2 plugin
- **Rationale**：ruff 速度优势；mypy strict 在 Pydantic v2 上误报多
- **Consequences**：少量 `cast` / `# type: ignore` 在动态属性处

### ADR-003：测试用 transport 抽象 + recorded fixture
- **Decision**：`TushareTransport` / `LLMTransport` 接口；测试注入 `FixtureTransport` / `RecordedTransport`
- **Rationale**：不烧积分；可重放；CI 不依赖外部
- **Alternatives**：pytest-vcr、responses
- **Consequences**：录制脚本需要维护；fixture 文件入库

### ADR-004：v0.1 不做 sample-mode
- **Decision**：v0.1 仅在测试中用 fixtures；用户必须配置真实 token
- **Rationale**：范围控制
- **Consequences**：无 token 的开发者评估需要先获取 token；后续 v0.4 考虑加 `--sample-mode`

### ADR-005：dashboard 不做自动化视觉测试
- **Decision**：标 `@pytest.mark.manual`；事件流逻辑用单元测试覆盖
- **Rationale**：TUI 视觉测试 ROI 低
- **Consequences**：dashboard 视觉错误依赖手工验证清单

### ADR-006：logging 走 stderr，questionary 走 stdin/stdout
- **Decision**：`logging.basicConfig(stream=sys.stderr)`；Rich `Console(stderr=True)` 用于 dashboard
- **Rationale**：避免与 questionary 抢 stdout
- **Consequences**：用户管道命令时 log 不进 stdout（可接受）

### ADR-007：Bootstrap 通过 fake_strategy 解决
- **Decision**：`tests/_fake_strategy/` 作为最小插件，用于 V0.5 / V0.6 自测
- **Rationale**：不让 V0.5 等待 V0.7 完成

### ADR-008：v0.1 不做 CI（GitHub Actions）
- **Decision**：本地 pytest + pre-commit
- **Rationale**：个人 / 小团队工具；公开后再加
- **Consequences**：贡献者需要本地跑全套验证

### ADR-009：migrations 文件命名 `YYYYMMDD_NNN_description.sql`
- **Decision**：如 `20260427_001_init.sql`
- **Rationale**：时间序 + 序号；DESIGN §8.2 已定义

### ADR-010：测试比例 70% 单元 / 30% 整合
- **Decision**：单元测试覆盖纯逻辑；整合测试覆盖端到端 pipeline
- **Rationale**：单元快、易调试；整合保证事实正确

### ADR-011：录制脚本 `scripts/record_*_fixture.py`
- **Decision**：手动触发的 Python 脚本（非 pytest 一部分）
- **Rationale**：录制时需要真实 token，不应在 CI / 自动化跑

---

## 6. 测试策略详解

### 6.1 fixture 目录结构

见 §2.1 项目骨架的 `tests/fixtures/`。约定：

- 文件名：`<api_or_stage>_<scenario>[_<extra>].json`
- 头部前 5 行为 JSON 注释段（用 `_meta` key）：录制日期 / 录制者 token 后 4 位（脱敏）/ 接口版本 / 备注
- 故障 fixture 放在 `_faults/` 子目录

### 6.2 录制 / 回放约定

```bash
# 录制
$ uv run python scripts/record_tushare_fixture.py \
    --api limit_list_d --trade-date 20260427 \
    --output tests/fixtures/tushare/limit_list_d_20260427.json

# 测试自动回放（无网络调用）
$ uv run pytest -q
```

录制脚本需要：
- 读真实 token
- 调用真实 SDK
- 写出 fixture（脱敏 token、保留 schema）
- 在 fixture 头部写入 `_meta` 段

### 6.3 故障注入 fixture 清单（13 项，对应 §13.1）

1. `tushare_429.json` → 限流
2. `tushare_5xx_with_state_ok.json` → fallback 成功
3. `tushare_5xx_with_state_failed.json` → 必需 → run failed / 可选 → continue
4. `tushare_unauthorized_required.json` → run failed
5. `tushare_unauthorized_optional.json` → continue + data_unavailable
6. `tushare_row_count_zero.json` → 空候选合法
7. `llm_json_invalid.json` → 重试 1 次
8. `llm_pydantic_invalid.json` → 重试 1 次
9. `llm_set_mismatch.json` → 重试（temp 0）
10. `llm_all_retries_failed.json` → batch failed → run partial_failed
11. `intraday_cache_in_eod_run.json` → 自动重拉
12. `force_sync_ignores_cache.json` → 忽略所有缓存
13. `keyboard_interrupt_during_run.json` → run cancelled

### 6.4 手工验证清单（每次 release 前过一遍）

- [ ] dashboard 在 Windows Terminal 渲染正确
- [ ] dashboard 在 PowerShell 5.1 / cmd 渲染（应 fallback 简单字符）
- [ ] EVA 主题色在浅色 / 深色背景下可读
- [ ] questionary 与 Live 切换无 stdout 冲突
- [ ] partial_failed 红字横幅可见
- [ ] intraday 黄字横幅可见
- [ ] 红黄横幅可叠加
- [ ] Ctrl+C 中断后再次运行能正常进行（DB 不损坏）
- [ ] keyring 不可用时降级到 plaintext 有显式警告
- [ ] reports/ 目录文件齐全可读

### 6.5 覆盖率门槛

- v0.1 不强制 CI 覆盖率
- 关键模块自检目标：
  - `plugin_manager` / `strategy_runner` / `tushare_client` / `deepseek_client` / `pipeline`：≥ 85%
  - `data.py` / `prompts.py` / `render.py`：≥ 75%
  - `tui/*` 不强制（manual）

---

## 7. 风险登记

| ID | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | tushare 接口字段在录制日期与官方文档间漂移 | 中 | `data.py` 用 `df.get(col, default)` 容错；fixture 文件头记录录制日期；定期重录 |
| R2 | DeepSeek V4 API 变更 | 中 | profile 配置层抽象；recorded fixture 不依赖具体模型 ID；`DeepSeekClient` 仅依赖 OpenAI 兼容协议 |
| R3 | DuckDB 跨版本 SQL 不兼容 | 低 | pin 版本到 1.x；schema_migrations 表记录确认 |
| R4 | keyring 在 Linux headless / Docker 不可用 | 中 | 默认降级到 plaintext + 显式警告；测试中显式覆盖两条路径 |
| R5 | 主板筛选逻辑遗漏边缘股票 | 中 | `market='主板' AND exchange in ('SSE','SZSE')` 双条件；e2e 测试覆盖 |
| R6 | LLM 输出在不同温度下不稳定 | 高 | temperature 0.0-0.2；`response_format=json_object` + Pydantic 双校验；recorded fixture 兜底；运行时集合一致性强校验 |
| R7 | dashboard 在某些终端崩溃 | 低 | 手工验证清单 + `force_terminal=True`；崩溃时 fallback 到纯日志 |
| R8 | V0.0 骨架被遗漏直接进 V0.1 | 中 | 本计划将 V0.0 列为必经迭代 |
| R9 | tushare fixture 录制成本失控 | 中 | 优先录核心 6 个接口的 1 个典型日；故障 fixture 用衍生编辑生成 |
| R10 | pipeline 跨批 final_ranking 复杂度低估 | 中 | V0.7b 单独估算 1.5 人日；如延期可只发 v0.1 单批版本，多批延后 |
| R11 | Windows 路径 / 权限问题 | 中 | `pathlib.Path` + 测试用 `tmp_path`；README 提示避免 OneDrive 同步路径 |
| R12 | 估算偏差累积 | 中 | 每完成 V0.x 回顾实际工时，超 +30% 时立即调整后续估算 |

---

## 8. 调试与开发环境

### 8.1 一键启动 dev 环境

```bash
# 首次
$ git clone <repo>
$ cd deeptrade
$ uv sync --all-extras
$ uv run pre-commit install

# 日常
$ uv run pytest -q                          # 跑全部测试
$ uv run pytest -m "not manual" -q          # 跳过 manual 测试
$ uv run pytest tests/core/ -v              # 单模块
$ uv run pre-commit run --all-files         # 手工跑 pre-commit
$ uv run deeptrade <subcommand>             # 跑 CLI
```

### 8.2 环境变量（开发用）

```bash
DEEPTRADE_DB_PATH=./dev.duckdb              # 不污染 ~/.deeptrade
DEEPTRADE_LOG_LEVEL=DEBUG
DEEPTRADE_TUSHARE_TOKEN=xxx                  # 不走 secret_store
DEEPTRADE_DEEPSEEK_API_KEY=xxx
DEEPTRADE_DEEPSEEK_PROFILE=fast              # dev 时省钱
```

### 8.3 调试技巧

- 单元测试卡住：用 `-x --pdb` 进入断点
- LLM 失败：检查 `~/.deeptrade/deeptrade.duckdb` 中 `llm_calls.response_json`
- tushare 失败：检查 `tushare_calls` + `tushare_sync_state`
- TUI 渲染异常：临时设 `DEEPTRADE_FORCE_TERMINAL=0` 切回纯日志
- 录制新 fixture：`scripts/record_tushare_fixture.py` 或 `scripts/record_llm_fixture.py`

---

## 9. 估算汇总

| 迭代 | 人日 | 累计 |
|---|---|---|
| V0.0 项目骨架 | 0.5 | 0.5 |
| V0.1 DuckDB + secret_store | 1.0 | 1.5 |
| V0.2 Config CRUD | 1.0 | 2.5 |
| V0.3 TushareClient | 1.5 | 4.0 |
| V0.4 DeepSeekClient | 1.5 | 5.5 |
| V0.5 Plugin Manager + fake_strategy | 2.0 | 7.5 |
| V0.6 Dashboard + 事件流 | 1.5 | 9.0 |
| V0.7a LUB 数据层 | 2.5 | 11.5 |
| V0.7b LUB LLM 层 | 1.5 | 13.0 |
| V0.7c LUB 渲染与报告 | 1.0 | 14.0 |
| V0.8 容错与日志收尾 | 1.0 | 15.0 |
| V0.9 文档与样例 | 1.0 | 16.0 |
| 联调缓冲 | 1.5 | **17.5** |

实际进度可能因 R10 / R11 / R12 浮动 ±30%。每完成 V0.x 后回顾实际工时，超 +30% 立即调整剩余估算并通知用户。

---

## 10. 完成信号 / Release Gate（v0.1.0）

打 `v0.1.0` git tag 前必须满足：

- [ ] V0.0 → V0.9 全部通过 DoD
- [ ] 全部单元 + 整合测试 pass
- [ ] §6.4 手工验证清单全过
- [ ] 关键模块覆盖率达 §6.5 门槛
- [ ] DESIGN.md / PLAN.md 与代码同步（如有偏差，先在文档中记录）
- [ ] CHANGELOG.md 写入 v0.1.0
- [ ] README.md 五分钟可上手

满足全部即发布。

---

## 11. v0.6 LLM Manager 化（2026-05-01 加入）

> v0.1 MVP / v0.5 架构修订之后的下一轮破坏式重构。把 LLM 抬升为框架层基础能力（多 provider + 统一 manager）。规格见 DESIGN §0.7 + §10。

### 11.1 工作分解（按文件维度）

| # | 任务 | 估算 | 备注 |
|---|---|---|---|
| T1 | `core/config.py`：删除 `deepseek_*` 字段；新增 `llm_providers` (dict)、`llm_audit_full_payload` (bool)；`SECRET_KEYS` 改为前缀匹配 `llm.*.api_key`；新增 `LLMConfig` Pydantic 模型 | 0.5 |  |
| T2 | `core/deepseek_client.py` → `core/llm_client.py`，`DeepSeekClient` → `LLMClient`；保留 `OpenAIClientTransport`；改所有 import | 0.3 | 纯改名 |
| T3 | `core/llm_manager.py`（新）：`LLMManager` + `LLMConfig` + `LLMProviderInfo` + `LLMNotConfiguredError`；按 `(name, plugin_id, run_id)` 缓存；非线程安全注释 | 0.5 |  |
| T4 | DB 迁移脚本（`migrations/<ver>_v06_llm_providers.sql` 或 Python migration）：旧 `deepseek.*` → `llm.providers["deepseek"]` + `llm.deepseek.api_key` + `llm.audit_full_payload`；幂等 | 0.3 |  |
| T5 | `cli_config.py`：删 `set-deepseek` / `cmd_test`；加 `set-llm`(交互新建/修改/删除) + `list-llm` + `test-llm [name]`；`show` 展开 `llm.providers` | 0.5 |  |
| T6 | `volume_anomaly/runtime.py` + `limit_up_board/runtime.py`：移除 `build_llm_client`；`llm: DeepSeekClient` → `llms: LLMManager`；调用点改 `rt.llms.get_client(name, plugin_id=, run_id=)`。各内建插件**先硬编码 `name="deepseek"`**，未来再加 `<plugin>.default_llm` 配置项 | 0.5 |  |
| T7 | 测试：`test_deepseek_client.py` → `test_llm_client.py`；新增 `test_llm_manager.py`（list_providers 过滤、get_client 缓存、缺 api_key、迁移）；更新所有 fixture 中 `deepseek.*` 写入路径 | 0.5 |  |
| T8 | `CHANGELOG.md` v0.6 条目；DESIGN.md 已改完 | 0.2 |  |
| **合计** |  | **3.3 人日** |  |

### 11.2 顺序与依赖

```
T1 (config schema)
  ├─→ T2 (rename client)
  ├─→ T4 (migration)
  └─→ T3 (LLMManager) ──┬─→ T6 (runtime collapse)
                        └─→ T5 (CLI rework)
                                ├─→ T7 (tests)
                                └─→ T8 (CHANGELOG)
```

### 11.3 验证 / DoD

- 单元：`test_llm_manager.py` 覆盖 list_providers 过滤、get_client 缓存、缺 api_key 抛错、provider 不存在抛错。
- 整合：`tests/cli/test_config_llm.py`（新）覆盖 `set-llm` 三种交互（新建/修改/删除）、`list-llm`、`test-llm` 单/全。
- 迁移：单元测试构造遗留 `deepseek.*` 行 → 应用迁移 → 断言新键存在、旧键删除、再次跑迁移幂等。
- 回归：现有所有 LLM 相关测试在改名后通过；`volume-anomaly` / `limit-up-board` 端到端 dispatch 跑通。
- ruff + mypy clean。

### 11.4 风险

| ID | 风险 | 概率 | 缓解 |
|---|---|---|---|
| RV6-1 | 迁移误判（已有 `llm.providers` 还把 `deepseek.*` 又迁一遍） | 低 | 迁移检查 `llm.providers` 是否非空；非空则跳过整段 |
| RV6-2 | `SECRET_KEYS` 前缀匹配把无关 key 误判为 secret | 中 | 严格只匹配 `llm.<name>.api_key` 模式（正则或两端边界检查），不放宽到 `llm.*.*key*` |
| RV6-3 | 缓存按 `(name, plugin_id, run_id)`，run_id 为 None 时多次构造同 client 也算独立项 | 低 | 文档注明；插件应在 dispatch 入口确定 run_id 后再 get_client |
| RV6-4 | `KNOWN_STAGES` 仍硬编码，与"框架层基础能力"提法冲突 | 低 | **已在 v0.7 偿还**：stage 概念彻底归插件，`StageProfile` 升格为 `plugins_api.llm` 公共契约；同步把 `deepseek.profile` 改名 `app.profile`、删 `llm_calls.stage` 列。详见 DESIGN §0.8 / §10.1 / §10.5、CHANGELOG v0.7.0 |
