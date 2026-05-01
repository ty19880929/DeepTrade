# DeepTrade 设计文档

> 基于 LLM 与多策略的 A 股沪深主板选股 CLI 工具
> 文档版本 v0.6 · 2026-05-01
>
> 免责声明：本工具仅用于策略研究、数据整理与候选标的分析，不构成投资建议，不进行自动交易。

---

## 0. v0.5 / v0.6 架构修订（破坏式重构，无兼容性）

> 本节是 v0.5 + v0.6 重塑的权威摘要。**与下文 §2–§18 中具体描述存在冲突时，以本节为准**；§2–§18 保留为历史设计参考与各子系统的细节说明。完整决策记录：v0.5 见 [`docs/plugin_cli_dispatch_evaluation.md`](docs/plugin_cli_dispatch_evaluation.md) v0.3；v0.6 LLM Manager 化见 §0.7 + §10。

### 0.1 框架命令面（封闭）

```
deeptrade --version | -V
deeptrade --help    | -h         # 仅展示框架命令；不枚举插件子命令
deeptrade init [--no-prompts]
deeptrade config {show, set, set-tushare, set-llm, list-llm, test-llm}
deeptrade plugin {install, list, info, enable, disable, uninstall, upgrade}
deeptrade data sync ...           # 临时禁用 stub，下版本恢复
deeptrade <plugin_id> <argv...>   # 纯透传：framework → plugin.dispatch(argv)
```

保留字（不可作为 plugin_id）：`init / config / plugin / data`。

### 0.2 插件契约（最小化）

```python
class Plugin(Protocol):                               # plugins_api.base
    metadata: PluginMetadata
    def validate_static(self, ctx: PluginContext) -> None: ...
    def dispatch(self, argv: list[str]) -> int: ...

class ChannelPlugin(Plugin, Protocol):                # plugins_api.channel
    def push(self, ctx: PluginContext, payload: NotificationPayload) -> None: ...
```

`PluginContext` 是框架在安装期 (`validate_static`) 与通知派发期 (`ChannelPlugin.push`) 提供的最小服务束：`db` + `config` + `plugin_id`。其它一切（TushareClient / DeepSeekClient / TUI / 运行历史表）由插件**在自己的 dispatch 内**自行构造。

被删除的旧契约：`StrategyPlugin` Protocol、`StrategyContext`、`StrategyParams`、`StrategyRunner`、TUI dashboard、`hello`、交互式主菜单、`strategy/channel` 命令组。

### 0.3 数据隔离（Plan A — 纯隔离）

框架 schema **仅** 9 张表：

```
app_config, secret_store, schema_migrations,
plugins, plugin_tables, plugin_schema_migrations,
llm_calls, tushare_sync_state, tushare_calls
```

- 没有任何业务表（`strategy_runs / strategy_events / stock_basic / trade_cal / daily / daily_basic / moneyflow` 全部从 core migrations 移除）。
- `tushare_sync_state` PK = `(plugin_id, api_name, trade_date)`。`tushare_calls` / `llm_calls` 加 `plugin_id` 列。每插件独立维护自己的 tushare 缓存与同步状态，**不跨插件共享**。
- 每个插件在自己的 `migrations/*.sql` + yaml `tables:` 里声明并拥有它需要的全部表（包括 tushare 派生数据，如 `lub_stock_basic` / `va_watchlist`）。即便不同插件需要相同 tushare 数据，**也各自落库一份**，不耦合复用。
- `TushareClient.__init__` 必填 `plugin_id`。框架级连通性测试（`config test`）使用哨兵 `FRAMEWORK_PLUGIN_ID = "__framework__"`。

### 0.4 通知 API（顶层）

```python
from deeptrade import notify, notification_session

notify(db, payload)                         # 一次性
with notification_session(db) as ns:        # 批量
    ns.push(p1); ns.push(p2)
```

框架根据已安装的 `type=channel` 插件自动路由。无 channel 启用 → NoopNotifier（零成本）。`build_notifier`/`MultiplexNotifier`/`AsyncDispatchNotifier` 仍在 `core/notifier.py`，但顶层直接 `from deeptrade import notify`。

### 0.5 内建插件

| plugin_id | type | version | 子命令（自管） |
|---|---|---|---|
| `limit-up-board` | strategy | 0.2.0 | `run / sync / history / report` |
| `volume-anomaly` | strategy | 0.2.0 | `screen / analyze / prune / history / report` |
| `stdout-channel` | channel | 0.1.0 | `test / log` + `push()` 实现 |

每个内建 strategy 插件结构：`plugin.py`（Plugin 入口）+ `cli.py`（typer 子命令）+ `runner.py`（生命周期）+ `runtime.py`（服务束）+ `migrations/<ver>_init.sql`（含全部前缀化的 tushare 表 + `<prefix>_runs` / `<prefix>_events`）。

### 0.6 与下文 §2–§18 的关系

下文章节按时间序保留（`§9` TUI dashboard、`§12` 打板策略、`§18` 通知子系统等）：作为各子系统的**实现细节参考**仍然有效，但其中下列概念已**不再存在**或已**变形**：

- `StrategyContext` / `StrategyParams` / `StrategyRunner` → 不存在；改为各插件自管
- `strategy_runs` / `strategy_events` 框架表 → 移到各插件的 `<prefix>_runs` / `<prefix>_events`
- §6.2 "Shared market tables"（`stock_basic / daily / ...`）→ 不再共享，每插件自带 `<prefix>_*`
- §9 Live TUI dashboard → 删除；插件自行打印进度
- `deeptrade strategy run` / `strategy history` / `strategy report` / `channel test` → 不存在；由插件自管
- `DeepSeekClient` 类与 `core/deepseek_client.py` 模块 → v0.6 改名为 `LLMClient` / `core/llm_client.py`，并由新 `LLMManager` 统一构造（详见 §0.7 与 §10）

阅读本文档时请把 §0 视作首要权威，§2–§18 视作各子系统设计意图与历史决策的注释。

### 0.7 v0.6 LLM Manager 化（多 Provider）

> 把 LLM 从"DeepSeek 专属客户端"抬升为**框架层基础能力**：多 provider 同时存在，按名取用；单一插件可同时调用多个 LLM。完整规格见 §10。

**核心变化：**

- 配置模型从 `deepseek.*` 改为 `llm.*`：
  - `llm.providers` (JSON dict, app_config) — `{name: {base_url, model, timeout}}`
  - `llm.<name>.api_key` (secret_store) — 每 provider 一把 key
  - `llm.audit_full_payload` (app_config bool) — 取代 `deepseek.audit_full_payload`
  - **没有 `llm.default`** — 调用方必须显式指定 provider 名
- 新增框架层服务 `core/llm_manager.py::LLMManager`：
  ```python
  class LLMManager:
      def list_providers(self) -> list[str]:                   # 仅返回 api_key 已配的
      def get_provider_info(self, name: str) -> LLMProviderInfo:  # name/model/base_url
      def get_client(self, name, *, plugin_id, run_id, reports_dir=None) -> LLMClient
  ```
  按 `(name, plugin_id, run_id)` 缓存客户端实例；**非线程安全**（doc 注明，并发场景需多实例或外部加锁）。
- `core/deepseek_client.py` → `core/llm_client.py`；`DeepSeekClient` → `LLMClient`；`OpenAIClientTransport` 名称保留（OpenAI 兼容协议 transport 是其真实身份）。
- 假定所有 provider 都走 OpenAI 兼容协议（DeepSeek/Qwen/Kimi/Doubao/GLM/Yi/SiliconFlow/OpenRouter 等）；真正异构（Anthropic native / Gemini native）等出现需求时再引入 `type=llm-transport` 插件类型，本版不做。
- Stage profile（`fast/balanced/quality`）保持全局，与 provider 正交：transport 对不支持的 thinking/reasoning_effort 字段静默丢弃。
- `KNOWN_STAGES` 仍硬编码（`strong_target_analysis / continuation_prediction / final_ranking`）—— **登记为已知技术债**，v0.7 已偿还（见 §0.8）。
- CLI：删除 `set-deepseek` 与 `config test`；新增 `set-llm`（交互式新建/修改/删除）、`list-llm`、`test-llm [name]`。
- 内建策略 `volume-anomaly` / `limit-up-board` 的 `runtime.py` 移除各自 `build_llm_client`；改用 `rt.llms: LLMManager` 字段，调用 `rt.llms.get_client(name, plugin_id=, run_id=)`。
- DB 迁移：开启时若发现遗留 `deepseek.*` 键，幂等迁移成 `llm.providers["deepseek"]` + `llm.deepseek.api_key` + `llm.audit_full_payload`，并删除遗留键。

### 0.8 v0.7 Stage 概念归插件 + 配置键改名

> 偿还 v0.6 RV6-4 / §10.2 已知技术债：把 stage 名字、preset → stage tuning 表彻底从框架剔除，归插件自维护；同步把全局 preset 键 `deepseek.profile` 改名为 vendor-agnostic 的 `app.profile`。

**核心变化：**

- **删除 `core.llm_client.KNOWN_STAGES` / `LLMUnknownStageError` / `_stage_profile()` / `_CURRENT_STAGE`**。框架对 stage 名字一无所知。
- `LLMClient.complete_json` 签名变化：删 `stage: str` 入参；新增必填 `profile: StageProfile`；调用方直接传入已解析的调参档。`LLMClient.__init__` 也删除 `profiles=` 入参，`LLMManager.get_client()` 不再绑 profile。
- **删除 `core.config.DS_STAGES` / `DeepSeekProfileSet` / `PROFILES_DEFAULT` / `ConfigService.get_profile()`**。
- `StageProfile` 升格为公共契约，搬到 `deeptrade.plugins_api.llm`；`from deeptrade.plugins_api import StageProfile` 是插件作者唯一需要 import 的 LLM 调参类型。
- 配置键改名：`deepseek.profile` → `app.profile`。`AppConfig.deepseek_profile` 字段同名重命名为 `app_profile`。preset 仍是 `Literal["fast","balanced","quality"]`，语义保持全局。
- DB 行幂等迁移：`config_migrations.migrate_legacy_deepseek_profile_key`。
- **环境变量直接断代**：`DEEPTRADE_DEEPSEEK_PROFILE` 不再被识别；启动时若旧 env 设而新 env 未设，`get_app_config()` 抛 `RuntimeError` 退出（避免静默回落到默认 "balanced"）。
- DB schema：新增 `20260501_002_drop_llm_calls_stage.sql` 删除 `llm_calls.stage` 列；`20260427_001_init.sql` 同步去掉该字段，让 fresh DB 直接落到目标状态。
- `RecordedTransport` 改为纯 FIFO：`register(response)` 不再携带 stage 标签。
- 内建插件改造：每个插件新增 `<plugin>/profiles.py`（preset → stage tuning 表 + `resolve_profile(preset, stage)` 解析函数）；`runner.py` 读取 `cfg.app_profile` 字符串，传给 pipeline；pipeline 调用 `resolve_profile()` 拿 `StageProfile`。volume-anomaly 借此把语义错误的 stage 名 `continuation_prediction` 改回 `trend_analysis`。

**插件作者契约变化（api_version 仍为 "1"）：**

```python
from deeptrade.plugins_api import StageProfile

# 插件本地 profiles.py:
PROFILES = {
    "fast":     {"my_stage": StageProfile(thinking=False, ...)},
    "balanced": {"my_stage": StageProfile(thinking=True, ...)},
    "quality":  {"my_stage": StageProfile(thinking=True, ...)},
}
def resolve_profile(preset: str, stage: str) -> StageProfile: ...

# pipeline 内部:
prof = resolve_profile(rt.config.get_app_config().app_profile, "my_stage")
obj, _ = llm.complete_json(system=..., user=..., schema=..., profile=prof)
```

---

## 1. 总览

DeepTrade 是一款 **本地运行** 的命令行选股工具，定位为"策略容器 + 数据底座 + LLM 引擎"：

- **数据底座**：tushare（8000 积分权限）作为唯一外部行情/基本面数据源；DuckDB 作为单文件本地仓库。
- **LLM 引擎**：DeepSeek V4（`deepseek-v4-pro` / `deepseek-v4-flash`，1M 上下文，最大输出 384K tokens）。
- **策略容器**：插件式策略框架，每个策略自带元数据、数据库表声明、独立执行流程，并适配统一接口。
- **首发策略**：`limit-up-board`（打板策略），双轮 LLM 漏斗：强势标的分析 → 连板预测。

设计原则（按优先级）：

1. **轻量** — 无服务进程、无容器、无后台守护；一条命令即可跑完。
2. **可扩展** — 策略插件可独立开发、本地安装、自动建表，不污染核心代码。
3. **可观察** — 借鉴 TradingAgents 的 Rich-TUI 看板，实时展示 LLM 调用、数据补齐、筛选漏斗。
4. **结构化** — LLM 响应一律 Pydantic 强约束，禁止自由文本，每条结论必带证据字段。
5. **可重放** — 每次运行落库（`strategy_events`、`llm_calls`、`tushare_sync_state`），并导出报告到 `~/.deeptrade/reports/<run_id>/`。

---

## 2. 需求合理性评估

| # | 用户原始要求 | 评估 | 处理 |
|---|---|---|---|
| 2.1 | tushare 8000 积分作为唯一数据源 | ✅ 合理。8000 积分覆盖首版打板策略大部分核心数据（`limit_list_d` / `limit_step` / `limit_cpt_list` / `limit_list_ths` / `dc_hot` 等）；公告（`anns_d`）、集合竞价（`stk_auction_o`）等作为可选增强，必须有降级路径。 | 直接采用；详见 §11 |
| 2.2 | LLM = `deepseek-v4-pro` | ✅ **已确认**。DeepSeek V4 系列正式发布，`deepseek-v4-pro`（1M 上下文，384K 输出，OpenAI 兼容协议，支持 JSON Output / 思维链 / Tool Calls），定价见 [DeepSeek pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)。`deepseek-chat` / `deepseek-reasoner` 已宣告将弃用。 | 默认 `model=deepseek-v4-pro`，`base_url=https://api.deepseek.com`；按 stage 配置思维链（profile：fast/balanced/quality，详见 §10）；硬约束**永不**注册 tool/function calls |
| 2.3 | DuckDB 持久化 | ✅ 合理 | 直接采用 |
| 2.4 | 参考 TradingAgents 外观与交互 | ✅ 合理 | 见 §9 |
| 2.5 | EVA 主题配色 | ✅ 合理 | design tokens 见 §9.2 |
| 2.6 | TUI：标准 TUI vs Rich-TUI dashboard | **推荐 Rich-TUI dashboard**。本工具是线性"配置→跑→看"工作流，不需要多窗口/键盘导航；Textual 调试成本高；Rich + questionary + Live 与 TradingAgents 一致 | 见 §9.1 |
| 2.7 | 单纯本地运行 | ✅ 合理 | 直接采用 |
| 2.8 | 插件类型当前只需"策略类" | ✅ 合理 | 元数据预留 `type` 字段，未来扩展数据源/通知/回测无需破坏性改动 |
| 2.9 | 策略元数据声明表，安装时自动建表 | ✅ 合理 | 见 §8.2 |
| 2.10 | **严禁**限制提交给 LLM 的候选数；**严禁** LLM 引用外部数据；强制 JSON 响应 | ✅ 关键约束 | 通过分批策略 + system prompt 硬约束 + Pydantic schema + 集合一致性校验落实，**永不**询问用户是否截断 |
| 2.11 | 严禁通过互联网搜索获取 tushare 接口文档 | ✅ 已遵循。所有 tushare 接口规范均直接抓取自 `https://tushare.pro/document/2` | 见 §11 / 附录 A |

**待澄清问题已闭环**（详见 §16），全部按用户答复落地：
- 沪深主板：仅 SSE/SZSE 主板，不含创业板/科创板/北交所
- 候选数严格无上限，绝不询问截断
- 未授权接口的处理：`required` 接口未授权 → 终止 run；`optional` 接口未授权 → 仅打提示后软跳过 + prompt 注入 `data_unavailable`
- "T 日 = 最近一个**已收盘**交易日"（默认按 18:00 阈值 + 数据可用性判断；可用 `--allow-intraday` 显式选择盘中模式），T+1 为其后第一个开市日

---

## 3. 技术栈

| 类别 | 选型 | 备注 |
|---|---|---|
| 语言 | Python 3.11+ | — |
| CLI 框架 | `typer` ≥ 0.12 | 子命令路由 |
| 交互输入 | `questionary` ≥ 2.0 | 单选/多选/文本/确认/密码 |
| TUI 渲染 | `rich` ≥ 13.7 | Console / Layout / Live / Panel / Table / Markdown / Spinner |
| 数据库 | `duckdb` ≥ 1.0 | 单文件 `~/.deeptrade/deeptrade.duckdb` |
| 数据源 SDK | `tushare` ≥ 1.4 | 官方 SDK |
| LLM SDK | `openai` ≥ 1.0 | OpenAI 兼容协议指向 DeepSeek |
| 数据建模 | `pydantic` ≥ 2.7 | LLM 响应 schema、配置 schema、元数据 schema |
| HTTP 重试 | `tenacity` | 指数退避 |
| 元数据 | `PyYAML` ≥ 6.0 | 解析 `deeptrade_plugin.yaml` |
| 密钥存储 | `keyring` ≥ 25.0 | 系统钥匙串；不可用时降级到本地明文（显式警告） |
| 数据帧 | `pandas` ≥ 2.2 | tushare 返回 DataFrame |
| 包管理 | `uv` 或 `pip install -e .` | 入口脚本 `deeptrade` |
| 测试 | `pytest` | — |
| 日志 | `logging` + Rich Handler | `~/.deeptrade/logs/*.log` |

---

## 4. 架构总览

```
┌──────────────────────────────  deeptrade CLI (Typer) ─────────────────────────────┐
│  init │ config {show/set/test} │ plugin {install/list/info/disable/uninstall}     │
│  data sync │ strategy {run/history/report}                                         │
└─────┬────────┬────────────────────┬──────────────────────────────────┬────────────┘
      │        │                    │                                  │
      ▼        ▼                    ▼                                  ▼
  ┌──────┐ ┌────────┐  ┌──────────────────┐               ┌──────────────────────┐
  │ Init │ │ Config │  │ Plugin Manager   │               │ Strategy Runner      │
  │ Wzd  │ │  Svc   │  │ (yaml/install/   │◀──────────────│ (collect params,     │
  │      │ │+keyring│  │  validate/disable│               │  drive Live TUI,     │
  └──┬───┘ └───┬────┘  │  /purge/migrate) │               │  persist events)     │
     │         │       └──────┬───────────┘               └────────┬─────────────┘
     ▼         ▼              ▼                                    ▼
  ┌────────────────────────────────────────────┐         ┌─────────────────────────┐
  │              Core Services                  │         │  Strategy Plugin        │
  │  ┌────────┐ ┌──────────┐ ┌───────────────┐ │         │  (e.g. limit-up-board)  │
  │  │ DuckDB │ │ Tushare  │ │ DeepSeek V4   │ │◀────────│  • metadata YAML        │
  │  │ Repo   │ │ Client   │ │ Client        │ │         │  • migrations.sql       │
  │  │+migr.  │ │ +ratelim │ │ +JSON+thinking│ │         │  • validate(ctx)        │
  │  └────────┘ │ +cache   │ └───────────────┘ │         │  • sync_data(ctx,p)     │
  │             │ +retry   │                   │         │  • run(ctx, p)          │
  │             └──────────┘                   │         │  • render_result(ctx,r) │
  └────────────────────────────────────────────┘         └─────────────────────────┘
```

**调用约束**：策略插件**只能**通过 `StrategyContext` 访问 Core Services。这样可以集中收口限流、缓存、API key 管理；插件不接触明文密钥。

---

## 5. 目录结构

### 5.1 用户目录

```text
~/.deeptrade/
├── deeptrade.duckdb              # 主数据库
├── config.toml                   # 引导配置（仅非敏感字段，如数据库路径）
├── logs/                         # 滚动日志
├── reports/<run_id>/             # 每次运行导出
│   ├── summary.md
│   ├── round1_strong_targets.json
│   ├── round2_predictions.json
│   └── llm_calls.jsonl
└── plugins/
    ├── installed/<plugin_id>/<version>/...   # 安装的插件包副本
    └── cache/                                # 临时下载缓存
```

### 5.2 项目源码

```text
deeptrade/
├── pyproject.toml
├── README.md
├── DESIGN.md                                  # 本文档
├── deeptrade/
│   ├── __init__.py
│   ├── cli.py                                 # Typer 入口
│   ├── theme.py                               # EVA design tokens
│   ├── tui/
│   │   ├── welcome.py                         # ASCII art + 欢迎屏
│   │   ├── question_box.py                    # create_question_box
│   │   ├── dashboard.py                       # Live Layout 看板
│   │   └── widgets.py
│   ├── core/
│   │   ├── paths.py
│   │   ├── db.py                              # DuckDB + migrations
│   │   ├── secrets.py                         # keyring + 加密
│   │   ├── config.py                          # 配置读写
│   │   ├── tushare_client.py                  # 限流/缓存/重试/审计
│   │   ├── deepseek_client.py                 # JSON + 思维链 + 校验
│   │   ├── plugin_manager.py                  # YAML 解析 + 安装/卸载
│   │   ├── strategy_runner.py
│   │   └── context.py                         # StrategyContext
│   ├── plugins_api/
│   │   ├── base.py                            # StrategyPlugin Protocol
│   │   ├── metadata.py                        # PluginMetadata Pydantic
│   │   └── events.py                          # StrategyEvent
│   └── strategies_builtin/
│       └── limit_up_board/                    # 内置示例插件
│           ├── deeptrade_plugin.yaml
│           ├── strategy.py                    # 入口类
│           ├── data.py                        # 数据装配
│           ├── prompts.py
│           ├── schemas.py                     # Pydantic 响应模型
│           ├── pipeline.py                    # 双轮筛选流程
│           ├── render.py
│           └── migrations.sql
└── tests/
```

---

## 6. 数据库设计（DuckDB）

数据库文件：`~/.deeptrade/deeptrade.duckdb`

### 6.1 核心表（由 `init` 创建）

```sql
-- 配置（非敏感）
CREATE TABLE IF NOT EXISTS app_config (
    key         VARCHAR PRIMARY KEY,
    value_json  VARCHAR NOT NULL,
    is_secret   BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 密钥（加密；如系统 keyring 不可用，is_plaintext=true 显式标注）
CREATE TABLE IF NOT EXISTS secret_store (
    key                VARCHAR PRIMARY KEY,
    encrypted_value    BLOB    NOT NULL,
    encryption_method  VARCHAR NOT NULL,    -- 'keyring' | 'plaintext'
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- schema 版本
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 已安装插件
CREATE TABLE IF NOT EXISTS plugins (
    plugin_id     VARCHAR PRIMARY KEY,
    name          VARCHAR NOT NULL,
    version       VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,            -- 当前固定 'strategy'
    api_version   VARCHAR NOT NULL,            -- 插件接口版本
    entrypoint    VARCHAR NOT NULL,            -- 'module.path:Class'
    install_path  VARCHAR NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_yaml VARCHAR NOT NULL,            -- 完整快照
    installed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 插件持有的表（卸载 --purge 时按此清理）
CREATE TABLE IF NOT EXISTS plugin_tables (
    plugin_id  VARCHAR NOT NULL,
    table_name VARCHAR NOT NULL,
    ddl        VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plugin_id, table_name)
);

-- 策略运行记录
-- status 取值由 Pydantic / Repository 层校验：
--   running | success | failed | partial_failed | cancelled
-- （不在 DDL 加 CHECK 约束，避免后续 ALTER 不平滑；DuckDB 修改 CHECK 需要建新表+复制+替换）
CREATE TABLE IF NOT EXISTS strategy_runs (
    run_id        UUID PRIMARY KEY,
    plugin_id     VARCHAR NOT NULL,
    trade_date    VARCHAR NOT NULL,
    status        VARCHAR NOT NULL,
    is_intraday   BOOLEAN NOT NULL DEFAULT FALSE,    -- 是否盘中模式（--allow-intraday）
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    params_json   VARCHAR,
    summary_json  VARCHAR,
    error         VARCHAR
);

-- 插件级 schema 迁移版本（与核心 schema_migrations 解耦，支持插件升级）
CREATE TABLE IF NOT EXISTS plugin_schema_migrations (
    plugin_id   VARCHAR NOT NULL,
    version     VARCHAR NOT NULL,                -- e.g. '20260427_001'
    checksum    VARCHAR NOT NULL,                -- sha256 of DDL content
    applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plugin_id, version)
);

-- 持久化事件流（含进度、tushare/llm 调用、错误）
CREATE TABLE IF NOT EXISTS strategy_events (
    run_id       UUID NOT NULL,
    seq          BIGINT NOT NULL,
    event_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level        VARCHAR NOT NULL,             -- info | warn | error
    event_type   VARCHAR NOT NULL,             -- step.start | progress | tushare.call | llm.call | result | log
    message      VARCHAR NOT NULL,
    payload_json VARCHAR,
    PRIMARY KEY (run_id, seq)
);

-- LLM 调用流水
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id           UUID PRIMARY KEY,
    run_id            UUID,
    plugin_id         VARCHAR,
    stage             VARCHAR,                 -- e.g. 'r1.batch.3' / 'r2.batch.1'
    model             VARCHAR,
    prompt_hash       VARCHAR,
    input_tokens      BIGINT,
    output_tokens     BIGINT,
    latency_ms        INT,
    request_json      VARCHAR,
    response_json     VARCHAR,
    validation_status VARCHAR,                 -- ok | retry | failed
    error             VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tushare 同步幂等状态
CREATE TABLE IF NOT EXISTS tushare_sync_state (
    api_name           VARCHAR NOT NULL,
    trade_date         VARCHAR NOT NULL,         -- '*' 表示无日期维度（如 stock_basic）
    status             VARCHAR NOT NULL,         -- ok | partial | failed | unauthorized
    row_count          BIGINT,
    cache_class        VARCHAR NOT NULL DEFAULT 'trade_day_immutable',
                                                 -- static | trade_day_immutable | trade_day_mutable | hot_or_anns
    ttl_seconds        INT,                      -- 仅 static 与 hot_or_anns 类型用
    data_completeness  VARCHAR NOT NULL DEFAULT 'final',
                                                 -- final | intraday
                                                 -- 盘中（--allow-intraday）模式同步的日终稳定数据写 'intraday'
                                                 -- 日终模式读取时拒绝 'intraday' 缓存，自动重拉
    synced_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (api_name, trade_date)
);

-- Tushare 调用流水（细粒度审计）
CREATE TABLE IF NOT EXISTS tushare_calls (
    api_name    VARCHAR,
    params_hash VARCHAR,
    rows        INT,
    latency_ms  INT,
    called_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 6.2 通用行情表（核心层共享，避免每个策略重复同步）

```sql
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code VARCHAR PRIMARY KEY,
    symbol VARCHAR, name VARCHAR, area VARCHAR, industry VARCHAR,
    market VARCHAR,           -- '主板' / '创业板' / '科创板' / 'CDR'
    exchange VARCHAR,         -- SSE / SZSE / BSE
    list_status VARCHAR, list_date VARCHAR, delist_date VARCHAR,
    is_hs VARCHAR, act_name VARCHAR, act_ent_type VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_cal (
    exchange VARCHAR, cal_date VARCHAR, is_open INT, pretrade_date VARCHAR,
    PRIMARY KEY (exchange, cal_date)
);

CREATE TABLE IF NOT EXISTS daily (
    ts_code VARCHAR, trade_date VARCHAR,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code VARCHAR, trade_date VARCHAR, close DOUBLE,
    turnover_rate DOUBLE, turnover_rate_f DOUBLE, volume_ratio DOUBLE,
    pe DOUBLE, pe_ttm DOUBLE, pb DOUBLE, ps DOUBLE, ps_ttm DOUBLE,
    total_share DOUBLE, float_share DOUBLE, free_share DOUBLE,
    total_mv DOUBLE, circ_mv DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
```

### 6.3 插件表命名

为便于第三方插件作者灵活命名（如打板策略偏好 `lub_*` 前缀），**不强制**框架级表名前缀；策略元数据中需在 `tables` 列出全部 DDL，安装时按事务执行，并在 `plugin_tables` 登记。卸载 `--purge` 时按此登记表精确清理。

约定（非强制）：建议用 `<plugin_short>_*` 前缀避免冲突，如 `lub_limit_list_d`、`lub_stage_results`。框架在安装时检测命名冲突并报错。

---

## 7. 配置管理

### 7.1 三类配置

| 类型 | 字段（key） | 存储位置 |
|---|---|---|
| 引导（非敏感） | `app.db_path`、`app.timezone`(`Asia/Shanghai`)、`app.locale`(`zh_CN`)、`app.log_level`、`app.close_after`(默认 `"18:00"`，§12.2 用于判定"已收盘"的时刻阈值) | `~/.deeptrade/config.toml` + `app_config` |
| 业务（非敏感） | `tushare.rps`(默认 6)、`tushare.timeout`、`llm.providers`(JSON dict — 见下)、`llm.audit_full_payload`(默认 false)、`app.profile`(`balanced`，全局 preset 名；per-stage tuning 由各插件 `profiles.py` 解析，见 §10.1) | `app_config` |
| 密钥 | `tushare.token`、`llm.<name>.api_key`（每 provider 一把） | `secret_store` + 系统 keyring；keyring 不可用时降级到 `plaintext` 模式（CLI 显式提示风险） |

**`llm.providers` 形态（v0.6）**：

```json
{
  "deepseek":  {"base_url": "https://api.deepseek.com",   "model": "deepseek-v4-pro", "timeout": 180},
  "qwen-plus": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen-plus", "timeout": 180}
}
```

- 名字（key）由用户决定，作为后续插件代码 `rt.llms.get_client(name=...)` 的标识。
- `api_key` 不放在此字典里，单独走 `llm.<name>.api_key` → secret_store。
- `SECRET_KEYS` 改为前缀匹配：以 `llm.` 开头且以 `.api_key` 结尾的任意 key 都视作 secret，由 ConfigService 路由到 secret_store。
- 详见 §10 的 `LLMManager` 接口与运行时语义。

### 7.2 优先级

环境变量 > `secret_store`/`app_config` > 代码默认值。

环境变量映射：`DEEPTRADE_TUSHARE_TOKEN`、`DEEPTRADE_LLM_<NAME>_API_KEY`(逐 provider) …（`DEEPTRADE_<KEY 全大写下划线>`）。

### 7.3 子命令

```text
deeptrade init                       # 初始化（建库、迁移、收集 token、连通性自检）
deeptrade config show                # 表格列出当前配置（密钥脱敏；llm.providers 展开列出每个 provider）
deeptrade config set <key> <value>   # 直接设置（脚本化场景）
deeptrade config set-tushare         # 交互式编辑 tushare 配置
deeptrade config set-llm             # 交互式新建 / 修改 / 删除某 provider
deeptrade config list-llm            # 列出所有可用 provider（即 api_key 已配置的）
deeptrade config test-llm [name]     # 不带 name 测全部，带则只测一个
```

---

## 8. 插件体系

### 8.1 插件包结构（标准模板）

```text
limit_up_board/
├── deeptrade_plugin.yaml           # 元数据（必需）
├── README.md
├── migrations.sql                  # 可被 yaml 引用的 DDL 总文件（可选）
└── limit_up_board/
    ├── __init__.py
    ├── strategy.py                 # 入口类
    ├── data.py
    ├── prompts.py
    ├── schemas.py
    ├── pipeline.py
    ├── render.py
    └── sync.py
```

### 8.2 元数据规范（YAML）

```yaml
# deeptrade_plugin.yaml
plugin_id: limit-up-board               # kebab-case, 全局唯一
name: 打板策略
version: 0.1.0
type: strategy                          # 当前仅支持 'strategy'
api_version: "1"                        # 插件接口契约版本
entrypoint: limit_up_board.strategy:LimitUpBoardStrategy
description: 基于涨停池、连板天梯、资金流和板块热度的强势标的分析与连板预测
author: DeepTrade

permissions:
  tushare_apis:
    required:                           # install 阶段不试探 tushare；run 前 validate 失败才中止
      - stock_basic
      - trade_cal
      - daily
      - daily_basic
      - stock_st
      - limit_list_d
      - limit_step                      # 升级为 required：连板天梯是 R2 市场温度核心输入
      - moneyflow
    optional:                           # 缺失则降级（必须有 fallback，详见 §11.3）
      - limit_list_ths
      - limit_cpt_list                  # 缺则用 lu_desc/industry 聚合 fallback
      - top_list
      - top_inst
      - ths_hot
      - dc_hot
      - stk_auction_o
      - anns_d
      - suspend_d
      - stk_limit
  llm: true                             # 是否需要 LLM
  llm_tools: false                      # 硬约束：插件不得为 LLM 注册任何 tool/function

migrations:                             # 插件级 schema 迁移，独立版本管理
  - version: "20260427_001"
    file: migrations/001_init.sql       # 相对于插件根目录
    checksum: "sha256:<...>"            # 安装时校验完整性
  # 后续升级追加新 version；老 version 不重复执行

tables:                                 # 仅声明插件拥有的表名 + 用途 + 卸载策略
                                        # ⚠ 不再内联 DDL：DDL 唯一来源是 migrations 段（详见下文）
  - name: lub_limit_list_d
    description: 涨停明细缓存（limit_list_d 落库）
    purge_on_uninstall: true
  - name: lub_limit_ths
    description: 同花顺涨停榜单缓存（limit_list_ths 落库）
    purge_on_uninstall: true
  - name: lub_stage_results
    description: 策略各阶段（R1/R2/final_ranking）结构化结果
    purge_on_uninstall: true
```

对应 Pydantic 校验模型：

```python
# deeptrade/plugins_api/metadata.py
from pydantic import BaseModel, Field
from typing import Literal

class TableSpec(BaseModel):
    """仅声明表的元信息；DDL 由 migrations 文件统一管理（不内联）。"""
    name: str
    description: str = ""
    purge_on_uninstall: bool = True        # plugin uninstall --purge 时是否 DROP

class MigrationSpec(BaseModel):
    version: str = Field(..., pattern=r"^\d{8}_\d{3,}$")    # 'YYYYMMDD_NNN'
    file: str                                                # 相对插件根目录的 .sql 文件
    checksum: str                                            # 'sha256:<hex>'

class TushareApiPermissions(BaseModel):
    required: list[str] = []
    optional: list[str] = []

class PluginPermissions(BaseModel):
    tushare_apis: TushareApiPermissions = TushareApiPermissions()
    llm: bool = False
    llm_tools: Literal[False] = False      # 硬约束：v0.3 起任何 true 值均拒绝安装

class PluginMetadata(BaseModel):
    plugin_id: str = Field(..., pattern=r"^[a-z][a-z0-9-]{2,31}$")
    name: str
    version: str
    type: Literal["strategy"] = "strategy"
    api_version: str
    entrypoint: str                        # 'module.path:Class'
    description: str
    author: str = ""
    permissions: PluginPermissions = PluginPermissions()
    tables: list[TableSpec]                # 仅声明表清单（用于 plugin_tables 登记 + purge）
    migrations: list[MigrationSpec]        # **唯一** DDL 执行源；首次安装即按 version 顺序执行

    @model_validator(mode="after")
    def _migrations_cover_tables(self):
        # 安装时框架会进一步校验 migrations 执行后实际创建的表 ⊇ tables 声明的表名
        if not self.migrations:
            raise ValueError("metadata.migrations 不能为空：DDL 必须通过 migrations 管理")
        return self
```

### 8.3 安装 / 校验 / 运行三阶段分层

每阶段职责严格分离，**install 阶段不访问任何外部 API**，避免网络/权限故障阻塞安装。

| 阶段 | 触发时机 | 行为 | 不做 |
|---|---|---|---|
| `install` | `plugin install` | metadata 解析 + Pydantic 校验 + checksum 校验 + migrations 执行 + entrypoint import | 不调用 tushare、不调用 LLM、不试探外部权限 |
| `validate` | `plugin info` 手动；`strategy run` 自动 | **仅测连通性**：`pro.stock_basic(limit=1)` 探针 + DeepSeek 1-token echo | 不对每个 required API 单独 probe；validate 成功 ≠ 所有接口已可用 |
| `run` | `strategy run` 实际执行时 | 数据装配阶段对每个 required API 实际调用，任一失败立即终止 run（status=failed） | — |

```text
deeptrade plugin install <local_path>
  ① 解析 deeptrade_plugin.yaml → Pydantic 校验
  ② plugin_id 唯一性 / api_version 兼容性 / llm_tools 必须为 false
  ③ 拷贝插件到 ~/.deeptrade/plugins/installed/<plugin_id>/<version>/
  ④ 计算每个 migration 文件的 sha256 与 metadata.migrations[*].checksum 比对
  ⑤ 在事务中：
      - 按 version 顺序执行 migrations 中尚未 applied 的 SQL 文件
        （依据 plugin_schema_migrations 跳过已记录版本，支持 upgrade 增量执行）
      - 校验：执行后实际存在的表 ⊇ metadata.tables 声明的表名
      - 写入 plugins / plugin_tables（仅表名 + description + purge_on_uninstall）
      - 写入 plugin_schema_migrations（含 version + checksum）
  ⑥ import entrypoint，调用 plugin.validate_static(ctx)（不联网的静态自检）
  失败任意步骤：事务回滚、清理拷贝目录

⚠ DDL 唯一执行源：metadata.tables 不再含 ddl 字段；migrations 是首次安装与升级
  共用的唯一 DDL 执行通路。这避免了"内联 DDL vs migrations"双重来源带来的歧义。

deeptrade plugin list                  # plugin_id / name / version / enabled / validation
deeptrade plugin info <plugin_id>      # 元数据 + 表 + 最近 5 次 run + validation_status
                                       #   validation_status: not_validated | ok | failed (reason)
                                       #   info 触发 validate（联网试探，不影响 install 状态）
deeptrade plugin disable <plugin_id>   # enabled = FALSE，不删数据
deeptrade plugin enable <plugin_id>
deeptrade plugin uninstall <plugin_id> [--purge]
  默认：仅 disable + 删除 install_path 副本（保留表与历史 run）
  --purge：先打印待删表的行数清单 → 二次确认
        → DROP 所有 plugin_tables 中登记的表
        → 删 plugins / plugin_tables / plugin_schema_migrations 行

deeptrade plugin upgrade <local_path>  # 同 plugin_id 不同 version；仅追加新 migrations
```

**安全约束**：

- **仅支持本地 path 安装**，不内置远程下载；用户必须手动 git clone 后再 `install`。
- 安装前 CLI 必须显示：插件元数据摘要、entrypoint、required/optional 接口、待执行 migrations、表清单 → 用户确认。
- `permissions.llm_tools=true` 的元数据安装拒绝（v0.3 硬约束）。
- 插件代码不接触明文密钥；只能通过 `StrategyContext` 调用 Tushare/LLM。

### 8.4 策略选择 / 执行

```text
deeptrade strategy run [<plugin_id>]   # 不传 → questionary 单选
                                       # → 进入 plugin.configure() 收参 → run() → 看板
deeptrade strategy history             # 历史 run 列表
deeptrade strategy report <run_id>     # 重看指定历史报告
```

### 8.5 接口契约（api_version: "1"）

```python
# deeptrade/plugins_api/base.py
from typing import Protocol, Iterable
from pydantic import BaseModel
from .events import StrategyEvent

class StrategyParams(BaseModel):
    trade_date: str | None = None              # None = 自动取最近已收盘交易日
    allow_intraday: bool = False               # True 时盘中也用当日 T（详见 §12.2）
    force_sync: bool = False                   # 强制重新同步（忽略本地缓存）

class StrategyPlugin(Protocol):
    metadata: "PluginMetadata"

    def validate_static(self, ctx) -> None: ...     # install 后自检，禁止联网
    def validate(self, ctx) -> None: ...            # run 前联网自检（必需接口可用）
    def configure(self, ctx) -> dict: ...           # 交互式收参，返回 params dict
    def sync_data(self, ctx, params: StrategyParams) -> Iterable[StrategyEvent]: ...
    def run(self, ctx, params: StrategyParams) -> Iterable[StrategyEvent]: ...
    def render_result(self, ctx, run_id: str) -> None: ...
```

**`StrategyContext` 不暴露任何 LLM tool/function 调用接口**。插件可访问：

- `ctx.tushare.call(api_name, **params)` — 受控的 tushare 调用（限流 / 缓存 / 审计）
- `ctx.llm.complete_json(system, user, schema, *, stage)` — JSON-only LLM；**无** `chat_with_tools`
- `ctx.db` — 只读 / 受控写入（事务由 runner 统一管理）
- `ctx.emit(event)` — yield StrategyEvent 的语法糖

#### StrategyEvent 类型（枚举）

```python
class EventType(str, Enum):
    # 步骤生命周期
    STEP_STARTED       = "step.started"
    STEP_PROGRESS      = "step.progress"
    STEP_FINISHED      = "step.finished"
    # 数据同步
    DATA_SYNC_STARTED  = "data.sync.started"
    DATA_SYNC_FINISHED = "data.sync.finished"
    # Tushare
    TUSHARE_CALL       = "tushare.call"
    TUSHARE_FALLBACK   = "tushare.fallback"        # 落到本地新鲜度满足的缓存
    TUSHARE_UNAUTH     = "tushare.unauthorized"
    # LLM
    LLM_BATCH_STARTED  = "llm.batch.started"
    LLM_BATCH_FINISHED = "llm.batch.finished"
    LLM_FINAL_RANK     = "llm.final_ranking"        # R2 全局校准调用（详见 §12.5）
    VALIDATION_FAILED  = "validation.failed"        # JSON / Pydantic / 集合一致性校验失败
    # 结果
    RESULT_PERSISTED   = "result.persisted"
    LOG                = "log"
```

所有事件由 runner 持久化到 `strategy_events`，并实时驱动 TUI。

---

## 9. CLI 与 TUI 设计

### 9.1 TUI 选型决策

**采用 Rich-TUI 看板（参照 TradingAgents），不上 Textual。**

| 维度 | Rich + questionary + Live | Textual |
|---|---|---|
| 开发量 | 低 | 中（事件循环、Widget 注册） |
| 工作流契合 | 高（线性问卷 → 跑流程 → 结果） | 适合多视图切换 |
| Windows 终端兼容 | 优 | 中 |
| 调试 | print/log 即可 | 需 textual devtools |
| 维护成本 | 低 | 中 |

策略插件不暴露 Layout 控制权，仅通过 `yield StrategyEvent` 与看板通信，避免不同插件作者写出风格分裂的界面。

### 9.2 EVA 主题 Design Tokens

EVA 视觉的核心是 **深紫黑底 + 荧光绿/橙的高对比强调**。在 `deeptrade/theme.py` 集中管理（保持克制，避免装饰过度影响信息密度）：

```python
# deeptrade/theme.py
from rich.theme import Theme

# === EVA-01 design tokens ===
EVA_BG          = "#0B0B10"   # 黑底
EVA_PANEL       = "#15131D"   # 面板背景
EVA_PURPLE      = "#6B2FBF"   # 主框线（EVA-01 紫）
EVA_DEEP_PURPLE = "#3B0764"
EVA_LIME        = "#78D64B"   # 终端绿，成功/正常
EVA_ORANGE      = "#FF8A00"   # NERV 橙，进行中
EVA_RED         = "#E53935"   # 错误
EVA_YELLOW      = "#FFB000"   # 警告
EVA_TEXT        = "#E8E6F0"
EVA_DIM         = "#8C8799"
EVA_STOCK_UP    = "#E53935"   # A 股约定红涨
EVA_STOCK_DOWN  = "#78D64B"   # 绿跌

EVA_THEME = Theme({
    "title":                 f"bold {EVA_LIME}",
    "subtitle":              f"italic {EVA_DIM}",
    "panel.border.primary":  EVA_PURPLE,
    "panel.border.warn":     EVA_YELLOW,
    "panel.border.error":    EVA_RED,
    "panel.border.ok":       EVA_LIME,
    "status.pending":        EVA_DIM,
    "status.running":        EVA_ORANGE,
    "status.success":        EVA_LIME,
    "status.error":          EVA_RED,
    "k.value":               EVA_LIME,
    "k.label":               EVA_DIM,
    "stock.up":              EVA_STOCK_UP,
    "stock.down":            EVA_STOCK_DOWN,
    "spinner":               EVA_ORANGE,
    "headline.alert":        f"bold {EVA_YELLOW} on {EVA_DEEP_PURPLE}",
})
```

约定：所有 `Panel`、`Table`、`Text` 一律走主题命名样式，禁止直写颜色。

### 9.3 总体页面流

```
[deeptrade]
   │
   ├─► (库不存在?) ─► init wizard ──┐
   │                                  │
   ▼                                  ▼
┌──── Welcome Screen (EVA ASCII) ────┐
│ 版本 / 数据库 / 已装策略数 / 公告   │
└────────────────────────────────────┘
   │
   ▼
[主菜单] ① 配置  ② 插件  ③ 同步数据  ④ 运行策略  ⑤ 历史结果  ⑥ 退出
                          │                  │
                          ▼                  ▼
                  数据预同步流程       策略选择 (questionary)
                                              │
                                              ▼
                                      策略 configure() 子问卷
                                              │
                                              ▼
                                  ┌──── Live Dashboard ────┐
                                  │ Header: 策略 + run_id  │
                                  │ Progress: 阶段表       │
                                  │ Messages: 事件流       │
                                  │ Analysis: 阶段输出     │
                                  │ Footer: LLM/TS/Tokens  │
                                  └────────────────────────┘
                                              │
                                              ▼
                                  策略 render_result() →
                                  导出到 reports/<run_id>/
                                              │
                                              ▼
                                          返回主菜单
```

### 9.4 看板 Layout

```
┌──────────────────────── Header (EVA 紫框) ──────────────────────────┐
│  NERV ▌ DEEPTRADE   limit-up-board   T=20260427   RUN-01J...       │
├──────────────────────────────────┬─────────────────────────────────┤
│  Progress (阶段 × 状态)           │  Events                         │
│  ✔ 0 确定 T 日                    │  20:31 拉取 limit_list_d        │
│  ◐ 1 数据补齐 (8/12)              │  20:31 LLM r1.batch 1/7         │
│  ○ 2 R1 强势分析                  │  20:32 JSON validation passed   │
│  ○ 3 R2 连板预测                  │                                 │
├──────────────────────────────────┴─────────────────────────────────┤
│  Analysis （阶段性 Markdown / Table）                                │
└────── Footer: LLM 8 │ Tokens 320k↑/18k↓ │ TS 13 │ ⏱ 04:38 ────────┘
```

---

## 10. LLM 接入（v0.6 多 Provider + Manager）

> 抬升为框架层基础能力。多 provider 同时存在，由框架统一管理，插件按名取用。OpenAI 兼容协议作为单一 transport（DeepSeek/Qwen/Kimi/Doubao/GLM/Yi/SiliconFlow/OpenRouter 等都覆盖）；真正异构协议未来用 `type=llm-transport` 插件接入，本版不做。

### 10.0 LLMManager（框架对插件的接口）

`core/llm_manager.py` 提供唯一的 LLM 入口：

```python
class LLMNotConfiguredError(Exception): ...

@dataclass(frozen=True)
class LLMProviderInfo:
    name: str
    model: str
    base_url: str

class LLMManager:
    """框架层 LLM 管理器；插件通过 PluginContext / runtime 拿到此实例。

    非线程安全：缓存的 LLMClient 在单线程顺序调用下复用 transport，多线程并发
    需要外部加锁或为每线程构造独立 LLMManager。
    """

    def __init__(self, db: Database, config: ConfigService) -> None: ...

    def list_providers(self) -> list[str]:
        """返回所有【可用】 provider 名称（即 llm.providers 配置完整且
        llm.<name>.api_key 已设）。缺 api_key 的 provider 不会出现在列表中。"""

    def get_provider_info(self, name: str) -> LLMProviderInfo:
        """元信息（不含 api_key），可日志可展示。未配置抛 LLMNotConfiguredError。"""

    def get_client(
        self,
        name: str,
        *,
        plugin_id: str,
        run_id: str | None = None,
        reports_dir: Path | None = None,
    ) -> LLMClient:
        """返回直接可调用 complete_json(...) 的 LLMClient。
        缓存 key = (name, plugin_id, run_id)：一次 run 内多次取同名 provider
        复用 transport，避免重建 httpx 连接池。
        未配置 / 缺 api_key 抛 LLMNotConfiguredError。"""
```

**关键设计取舍：**

- **没有 `default` provider**：插件每次调用必须显式给名字。框架只管"清单 + 按名取"，"哪个 provider 是默认"是插件的偏好（插件可以读自己的 `<plugin>.default_llm` 配置项实现）。
- **`list_providers()` 过滤掉缺 api_key 的项**：语义是"可用清单"，避免插件拿到一个会 401 的名字。
- **缓存按 `(name, plugin_id, run_id)`**：一次 run 内的多次调用复用 transport。`plugin_id` 进 cache key 是为了 audit 隔离——不同插件的同名 provider 实例分开，`llm_calls.plugin_id` 列保持正确。

### 10.1 阶段级 Profile 配置（v0.7：归插件维护）

不同筛选阶段对成本/质量的偏好不同（R1 候选数大、需要稳定低成本；R2 候选少但决策关键，可开思维链；final_ranking 仅做排序合并，温度归零）—— 这种**阶段语义是插件域的知识，框架对其无感知**。

**预设档名仍框架级，"档 → 各 stage tuning" 表归插件**：用户在框架配置 `app.profile = balanced` 选择全局档位（`fast / balanced / quality`），由各插件本地的 `profiles.py` 把这个 preset 字符串解析成具体的 `StageProfile`。同一档名在不同插件里的具体 tuning 可以不同，因为不同插件的 stage 集合本身就不同。

```python
# deeptrade/plugins_api/llm.py（公共契约）
class StageProfile(BaseModel):
    thinking: bool
    reasoning_effort: Literal["low", "medium", "high"]
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(ge=1024, le=384_000)

# limit-up-board 的 profiles.py 示例
PROFILES: dict[str, dict[str, StageProfile]] = {
    "fast":     {"strong_target_analysis": ..., "continuation_prediction": ..., "final_ranking": ...},
    "balanced": {...},
    "quality":  {...},
}
def resolve_profile(preset: str, stage: str) -> StageProfile: ...
```

**Profile 与 provider 正交**：profile 描述"用法档位"（thinking / reasoning_effort / temperature / max_output_tokens），与具体厂商无关。transport 对当前 provider 不支持的字段静默丢弃（OpenAI o-系列、Qwen/Kimi 思维链开关支持各异）。

> R1/R2 默认 32k 输出预算的依据：单批 30 只候选 × 单只 ~600 tokens（含 evidence/rationale/risk_flags/missing_data + JSON 结构开销）≈ 18k；留 ~80% 安全垫即 32k。8k 默认值会必然触发 R1 输出截断 → JSON 失败 → partial_failed。具体 tuning 见各插件 `profiles.py`。

### 10.2 客户端实现

实际运行时由 `LLMManager.get_client(name, plugin_id=, run_id=)` 构造，插件不应直接 `import LLMClient` / `OpenAIClientTransport`。下面只展示关键接口骨架（实现见 `core/llm_client.py`）：

```python
# deeptrade/core/llm_client.py — v0.7
class OpenAIClientTransport(LLMTransport):
    """OpenAI 兼容协议 transport — 适配所有遵循 OpenAI Chat Completions
    协议的厂商（DeepSeek/Qwen/Kimi/Doubao/GLM/Yi/SiliconFlow/OpenRouter）。
    对当前 provider 不支持的字段（thinking 等）静默丢弃。"""
    def __init__(self, api_key: str, base_url: str, timeout: int) -> None: ...
    def chat(self, *, model, system, user, temperature, max_tokens,
             thinking, reasoning_effort) -> LLMResponse: ...

class LLMClient:
    """JSON-mode + 调用方传入的 StageProfile + 审计写入 llm_calls。
    构造由 LLMManager 完成，绑定 (provider_name, plugin_id, run_id, model)。
    v0.7：不再持有 profile 集，stage 概念归插件。"""
    def __init__(self, db, transport, *, model, plugin_id, run_id,
                 audit_full_payload=False, reports_dir=None) -> None: ...
    def complete_json(self, *, system: str, user: str,
                      schema: type[BaseModel], profile: StageProfile,
                      envelope_defaults: dict[str, Any] | None = None
                      ) -> tuple[BaseModel, dict[str, Any]]: ...
```

### 10.3 设计要点与硬约束

- 统一 `complete_json` 入口，**杜绝**自由文本响应。
- 插件**不直接构造** `LLMClient` / `OpenAIClientTransport`；唯一入口是 `LLMManager.get_client(name, ...)`。框架因此可在不破坏插件 API 的情况下未来引入 transport 插件类型（`type=llm-transport`）。
- `response_format={"type":"json_object"}` + Pydantic 双重校验；JSON 解析或 schema 校验失败 → 单次重试（带针对性 hint）→ 仍失败抛 `LLMValidationError` / `LLMEmptyResponseError`，runner 按 §13 partial_failed 处理。
- **永不传** `tools` / `tool_choice` / `functions` 参数 —— 模型无外部数据通道。
- **永不暴露** `chat_with_tools` / `register_tool` 之类的接口给插件作者。
- `permissions.llm_tools=true` 的元数据 → 安装拒绝（§8.3）。
- 调用方传入 `profile: StageProfile`（由插件本地 `profiles.resolve_profile(preset, stage)` 解析得到），调用前后写 `llm_calls`（含 `plugin_id` / `prompt_hash` / `validation_status` / `model` / token 计数；v0.7 起**不再有 stage 列**）。`audit_full_payload=False` 时 DB 行只存摘要，但 `~/.deeptrade/reports/<run_id>/llm_calls.jsonl` 永远写完整 prompt + response。
- **EvidenceItem 必须带 unit**：值 + 单位（如 `value=3.2, unit="亿"`）让证据自包含含义；prompt 装配时由 §11 数据层从 raw 单位生成 normalized 字段，DB 表保留 raw 单位（详见 §11.4）。

### 10.4 插件使用示例（v0.7 推荐用法）

```python
from deeptrade.plugins_api import StageProfile
from .profiles import resolve_profile  # 插件本地 preset → stage tuning 表

# 在插件 runtime 内（构造一次，整个 run 复用）
@dataclass
class VaRuntime:
    db: Database
    config: ConfigService
    llms: LLMManager        # ← 由插件 dispatch 入口注入
    plugin_id: str = PLUGIN_ID
    run_id: str | None = None
    ...

# 在 pipeline 内（同一 plugin 同时调多个 LLM 的合法用法）
preset = rt.config.get_app_config().app_profile  # "balanced" / "fast" / "quality"

analyser = rt.llms.get_client("deepseek",  plugin_id=rt.plugin_id, run_id=rt.run_id)
ranker   = rt.llms.get_client("qwen-plus", plugin_id=rt.plugin_id, run_id=rt.run_id)

evidence, _ = analyser.complete_json(
    system=..., user=..., schema=R1Schema,
    profile=resolve_profile(preset, "strong_target_analysis"),
)
ranking, _  = ranker.complete_json(
    system=..., user=..., schema=FinalSchema,
    profile=resolve_profile(preset, "final_ranking"),
)
```

可用 provider 清单（API 用法层面）：

```python
names = rt.llms.list_providers()      # → ["deepseek", "qwen-plus"]（仅 api_key 已配的）
info  = rt.llms.get_provider_info("deepseek")  # → LLMProviderInfo(name=, model=, base_url=)
```

### 10.5 配置迁移

**v0.5 → v0.6 自动**：DB 打开时执行幂等迁移，若发现旧 `deepseek.*` 键存在且 `llm.providers` 为空，则：

1. 构造一个名为 `deepseek` 的 provider 写入 `llm.providers`（`base_url` / `model` / `timeout` 来自旧键）。
2. 旧 `deepseek.api_key`（secret_store）→ 新 `llm.deepseek.api_key`（secret_store）。
3. 旧 `deepseek.audit_full_payload` → 新 `llm.audit_full_payload`。
4. 删除迁移过的旧键。

代码：`config_migrations.migrate_legacy_deepseek_keys`。

**v0.6 → v0.7 自动**：

1. DB 行：`deepseek.profile` → `app.profile`，幂等。代码：`config_migrations.migrate_legacy_deepseek_profile_key`。
2. DB schema：SQL migration `20260501_002_drop_llm_calls_stage.sql` 删除 `llm_calls.stage` 列。
3. **环境变量直接断代**：`DEEPTRADE_DEEPSEEK_PROFILE` 不再被识别。`ConfigService.get_app_config()` 启动时若检测到旧 env 设而新 env 未设，抛 `RuntimeError` 退出（避免静默回落到默认值）。请改为 `DEEPTRADE_APP_PROFILE`。

迁移幂等：再次启动时新键已存在，跳过。CHANGELOG 显式声明这是不兼容变更，但用户实际配置不丢失。

---

## 11. Tushare 数据访问层

### 11.1 客户端职责

- **限流**：令牌桶（默认 `tushare.rps=6`；被 429 后自适应减半并持久化新值）。8000 积分用户单接口可达 500 次/分钟，但保守起步 6 rps 足够覆盖打板策略全流程。
- **重试**：tenacity 指数退避，最大 5 次。
- **字段约束**：调用允许指定 `fields=` 减小载荷。
- **审计**：写 `tushare_calls`。
- **未授权**：API 提示"无权限"时 → `tushare_sync_state.status='unauthorized'`，事件流写 warn 级"接口未授权，已跳过"，仅在该接口属于 `required` 时才中止 run（详见 §13）。

#### 缓存策略：按数据类型分层（4 类）

| `cache_class` | 缓存键 | 刷新策略 | 典型接口 |
|---|---|---|---|
| `static` | `(api_name, '*')` | 7 天 TTL；`force_sync` 强刷 | `stock_basic`、`trade_cal`（增量按 cal_date） |
| `trade_day_immutable` | `(api_name, trade_date)` | `status=ok` 后**永不主动复刷**，除非 `force_sync` | `daily`、`limit_list_d`、`limit_step`、`limit_cpt_list`、`stock_st`、`top_list`、`top_inst`、`stk_limit` |
| `trade_day_mutable` | `(api_name, trade_date)` | 允许 T+1 / T+2 复刷（事后修正数据） | `moneyflow`、`daily_basic`（含因停复牌补登的修正） |
| `hot_or_anns` | `(api_name, trade_date)` | TTL（默认 6h） | `ths_hot`、`dc_hot`、`anns_d` |

读取流程：

```text
get(api_name, trade_date, *, is_intraday_run: bool):
  ① 查 tushare_sync_state[(api_name, trade_date)]
  ② 命中条件（同时满足）：
       - status='ok'
       - 按 cache_class 仍新鲜
       - 数据完整性匹配运行模式：
           is_intraday_run=False（默认日终模式）→ 必须 data_completeness='final'
           is_intraday_run=True（盘中）         → 接受 'intraday' 或 'final'
       命中 → 直接读对应业务表
  ③ 若 status='unauthorized' → 视 required 与否决定中止/降级
  ④ 否则：调 API → 写业务表 + 更新 sync_state
       data_completeness 写入规则：
         is_intraday_run=True 且 cache_class='trade_day_immutable' → 'intraday'
         否则                                                       → 'final'
  ⑤ 任意阶段写 tushare_calls 审计
```

**盘中模式隔离原则（F4 修复点）**：

- 盘中（`--allow-intraday`）拉取的 `limit_list_d / limit_step / limit_cpt_list / limit_list_ths / moneyflow / daily / daily_basic` 等本应在收盘后稳定的数据，写入时强制标 `data_completeness='intraday'`。
- 后续日终模式运行（无 `--allow-intraday`）读取这些键时**必须**拒绝命中，触发重拉并覆盖为 `'final'`。
- 这避免了"盘中 14:00 跑过一次 → 当晚 19:00 直接命中盘中残缺数据"的污染。
- UI / report：盘中模式产出的所有报告强制顶部黄字横幅 `INTRADAY MODE`，避免用户混用结果。

### 11.2 打板策略涉及接口（核对自官方文档）

#### 必需接口（`required`，缺失则中止）

| 接口 | 用途 | 关键字段 | 积分门槛 |
|---|---|---|---|
| `stock_basic` | 沪深主板过滤、行业、上市状态 | `ts_code,name,market,exchange,industry,list_status,list_date` | 2000 |
| `trade_cal` | 找最近交易日 / 前后交易日 | `cal_date,is_open,pretrade_date` | 2000 |
| `stock_st` | 排除 ST / *ST | ST 名单 | 3000 |
| `daily` | 价格、近 N 日走势 | `open,high,low,close,pre_close,pct_chg,vol,amount` | 基础 |
| `daily_basic` | 换手率、量比、市值、估值 | `turnover_rate,turnover_rate_f,volume_ratio,total_mv,circ_mv,pe,pb` | 2000 |
| `limit_list_d` | 涨停明细：封单、首末封板、炸板、连板 | `fd_amount,first_time,last_time,open_times,up_stat,limit_times,limit` | 5000 起可用；8000 解锁更高限流（500/分钟、不限量） |
| `limit_step` | 全市场连板天梯分布（R2 市场温度核心输入） | `nums` | 8000 |
| `moneyflow` | 当日 + 近 3 日资金流 | `net_mf_amount,buy_lg_amount,buy_elg_amount,sell_*` | 2000 |

#### 可选增强（`optional`，缺失则降级 + 提示）

| 接口 | 用途 | 关键字段 | 积分 |
|---|---|---|---|
| `limit_list_ths` | 涨停原因、标签、涨停成功率（同花顺） | `lu_desc,tag,limit_up_suc_rate,limit_order,lu_limit_order,free_float` | 8000 |
| `limit_cpt_list` | 当日强势板块（缺失时按 §11.3 fallback 链聚合） | `name,days,up_stat,cons_nums,up_nums,rank` | 8000 |
| `top_list` | 龙虎榜每日明细 | 净买入金额、上榜理由 | 2000 |
| `top_inst` | 龙虎榜机构席位 | 机构买卖 | 5000 |
| `ths_hot` | 同花顺人气榜 | 排名、概念 | 6000 |
| `dc_hot` | 东方财富人气榜 | 排名、概念 | 8000 |
| `stk_auction_o` | 开盘集合竞价（次日预判） | 集合竞价价格、量 | 视权限 |
| `anns_d` | 公告标题 + URL | `title,url` | 单独权限 |
| `suspend_d` | 停复牌 | `suspend_type` | 基础 |
| `stk_limit` | 涨跌停价（验证、次日预判） | `up_limit,down_limit` | 2000 |

> 缺失数据原则：
>
> - **必需接口缺失** → 终止运行 + 中文提示用户去 tushare 申请权限 + 给出文档 doc_id。
> - **可选接口缺失** → `tushare_sync_state` 标记 `unauthorized`；prompt 中显式写 `data_unavailable: [<api_name>, ...]`，让 LLM 知晓而非编造；UI Footer 黄字告警。

### 11.3 不能直接获取 / 可选缺失的数据 → 替代方案（不放弃）

| 需要的数据 | 问题 | 替代方案（必须落实） |
|---|---|---|
| 实时五档盘口、逐笔 | 收盘后接口不提供 Level-2 | `fd_amount` + `open_times` + `first_time/last_time` + `limit_order`/`lu_limit_order`（同花顺）近似封板强度 |
| 盘中实时题材发酵 | 本工具收盘后跑，且禁止联网 | `lu_desc`、`tag`（同花顺涨停原因） + `limit_cpt_list`（板块统计） + `ths_hot/dc_hot`（人气榜） |
| 新闻 / 市场传闻 | 用户禁止外部搜索；传闻不可验证 | 不采集；如 `anns_d` 有权限，仅取标题与 URL 元数据，不让 LLM 编造内容 |
| 次日开盘竞价强弱 | 运行时刻通常在 T 日盘后，无次日数据 | 主流程不依赖；v0.2 引入独立 `--mode auction-confirmation` 增强模式（在次日 9:25 后重跑） |
| **`limit_cpt_list` 未授权** | 缺板块当日热度排名 | 用 `limit_list_ths.lu_desc` / `tag` 聚合涨停家数；缺 ths 则退回 `stock_basic.industry` 聚合。prompt 注入 `sector_strength_source: "lu_desc_aggregation"` 或 `"industry_fallback"`，让 LLM 区分数据来源 |
| **`top_list` / `top_inst` 未授权** | 缺龙虎榜净买入与机构席位 | 不替代；prompt 注入 `data_unavailable: ["top_list"]`；不允许 LLM 推断"游资席位"等需要外部信息才能给出的论断 |
| **`ths_hot` / `dc_hot` 未授权** | 缺人气排名 | 不替代；同上注入 `data_unavailable` |
| **`limit_list_ths` 未授权** | 缺涨停原因 / 标签 / 涨停成功率 | `lu_desc` 退回 `stock_basic.industry`；`limit_up_suc_rate` 不替代；prompt 注入 `data_unavailable` |

### 11.4 单位与数值表达约定

为兼顾 token 经济与数据可追溯：

- **DB 表**：保留 tushare 原始单位与字段名（金额"元"或"万元"或"亿元"按 tushare 接口原义；不做转换）。
- **Prompt 装配**：由数据层（`data.py`）从 raw 字段计算 normalized 字段，仅供 prompt 使用：金额统一"亿"（`fd_amount_yi`、`amount_yi`、`circ_mv_yi`），资金净额"万"（`net_mf_amount_wan`），保留 2 位小数。
- **EvidenceItem**：`field` 必须引用 prompt 中**实际出现**的 normalized 字段名（如 `fd_amount_yi`）；`value` 为该字段值；`unit` 显式声明（如 `"亿"` / `"万"` / `"%"` / `"次"` / `"日"`）。

---

## 12. 打板策略详细设计

### 12.1 总体流程

```
                       ┌──────────────────────────────────┐
                       │ Step 0  确定 T 日                 │
                       │  T = 最近一个【已收盘】交易日       │
                       │   - 默认按 18:00 阈值 + 数据可用性  │
                       │   - --allow-intraday 可强制盘中     │
                       │  T+1 = trade_cal 中 T 后首个开市日  │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 1  数据装配 (R1 输入)        │
                       │  required 缺失即终止              │
                       │  optional 缺失走 §11.3 fallback   │
                       │  按 cache_class 4 类分层缓存       │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 2  R1: 强势标的分析（LLM）   │
                       │  stage=strong_target_analysis     │
                       │  按 token 预算分批（覆盖全部候选） │
                       │  集合一致性严格相等校验            │
                       │  汇总 selected=true → R2 输入     │
                       │  ⚠ 不做 R1 Reduce 全局再排序       │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 3  数据装配 (R2 增量)        │
                       │  仅对 R1 selected 的标的：         │
                       │   - limit_step (required) /        │
                       │     limit_cpt_list (optional+fb)   │
                       │   - top_list / top_inst (可选)    │
                       │   - moneyflow 近 5 日             │
                       │   - ths_hot / dc_hot (可选)       │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 4  R2: 连板预测（LLM）       │
                       │  stage=continuation_prediction    │
                       │  默认单批；token > 阈值时自动分批 │
                       │  分批 → batch_local rank          │
                       │  集合一致性严格相等校验            │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 4.5  Final Ranking 校准（LLM)│
                       │  仅当 R2 触发分批时才调用           │
                       │  stage=final_ranking              │
                       │  finalists = 各批 Top-K + 边界样本│
                       │  禁止新增事实/证据                │
                       │  输出 global final_rank           │
                       └──────────────┬───────────────────┘
                                      │
                                      ▼
                       ┌──────────────────────────────────┐
                       │ Step 5  结果展示与落库            │
                       │  Rich Table + Markdown            │
                       │  落 lub_stage_results + reports/  │
                       │  status: success/partial_failed/  │
                       │          failed/cancelled         │
                       └──────────────────────────────────┘
```

### 12.2 Step 0 算法（最近已收盘交易日）

打板策略依赖**收盘后稳定的**涨停池、炸板、封单、连板数据。盘中调用 `limit_list_d(today)` 通常不完整或缺失，会把不完整数据送入 LLM 产生看似结构化但事实基础不稳的结果。因此：

- **默认行为**：T = 最近一个**已收盘**交易日。
- **盘中模式**：仅当用户显式 `--allow-intraday`（CLI flag）或 `configure()` 中勾选时，才允许把当日（如开市）作为 T；UI 黄字告警"涨停池数据可能不完整"。

```python
from datetime import time

CN_TZ = "Asia/Shanghai"

def resolve_trade_date(now_dt, calendar, *, user_specified=None,
                       allow_intraday=False, close_after: time) -> tuple[str, str]:
    """返回 (T, T+1)。close_after 来自 app.close_after 配置（默认 18:00）。"""
    if user_specified:
        T = user_specified
        return T, calendar.next_open(T)

    today = now_dt.astimezone(CN_TZ)
    today_str = today.strftime("%Y%m%d")
    today_is_open = calendar.is_open(today_str)

    if today_is_open and (today.time() >= close_after or allow_intraday):
        # 已收盘 或 用户主动选择盘中
        T = today_str
    else:
        # 当天非交易日 / 盘中且未开启 allow_intraday → 取上一开市日
        T = calendar.pretrade_date(today_str)

    return T, calendar.next_open(T)
```

注意：

- `close_after` 默认 18:00（tushare `limit_list_d` 一般 17-18 点入库），可由 `app.close_after` 配置覆盖（评审 S2 修复点）。
- 当 `today.time() >= close_after` 但 `limit_list_d(today)` 同步返回空且非 unauthorized → 视为"tushare 数据尚未入库"，UI 红字提示：`"Tushare 数据可能尚未入库；可稍后重试，或用 --trade-date <YYYYMMDD> 显式指定前一交易日"`，run.status = `failed`（不进 partial_failed，因为这是数据准备阶段错误）。
- 用户也可通过 `--trade-date YYYYMMDD` 显式指定历史交易日（最常用于回看 / 调参）。

### 12.3 用户输入参数（`configure()`）

```text
- trade_date            默认 = 自动推断的 T；可手动改写为任意历史交易日
- allow_intraday        默认 False；True 时允许盘中将当日作为 T（UI 黄字告警）
- force_sync            是否强制重拉数据（默认 False）
- daily_lookback        历史日 K 回溯日数（默认 10）
- moneyflow_lookback    资金流回溯日数（默认 5）
- r1_batch_token_budget R1 每批 prompt token 预算上限（默认 80_000；用于动态切批）
- r2_batch_token_budget R2 每批 prompt token 预算上限（默认 200_000；超出则启用 R2 分批）
- include_optional_apis 可选接口启用清单（多选，默认全部）
- llm_profile           覆写 app.profile（默认继承全局；fast/balanced/quality）
```

> ⚠ **不存在"候选数上限"参数**。批次切分严格按 token 预算动态计算，确保所有候选都被分配到某个批次。

### 12.4 LLM 数据需求设计（强势标的分析 / R1）

#### 12.4.1 输入字段（每只候选股）

| 类别 | 字段 | tushare 来源 | 是否可得 | 替代 |
|---|---|---|---|---|
| 基础 | `ts_code, name, industry, market, list_date` | `stock_basic` | ✅ | — |
| 涨停质量 | `first_time, last_time, open_times, fd_amount, limit_amount, limit_times, up_stat` | `limit_list_d` | ✅ | — |
| 同花顺增强 | `lu_desc, tag, status, limit_order, lu_limit_order, limit_up_suc_rate, free_float` | `limit_list_ths` (可选) | ⚠ 视权限 | 缺失则 `data_unavailable` 注入 prompt |
| 量价 | 当日 `pct_chg, amount, turnover_rate`；近 5 日涨跌 + 成交额 | `daily` + `daily_basic` | ✅ | — |
| 市值流动性 | `total_mv, circ_mv, turnover_rate_f, volume_ratio` | `daily_basic` | ✅ | — |
| 资金 | `net_mf_amount, buy_lg_amount, buy_elg_amount`，当日 + 近 3 日 | `moneyflow` | ✅ | — |
| 板块强度 | 涨停原因 / 板块排名 / 涨停家数 / 连板家数 | primary: `limit_cpt_list`；fallback_1: `limit_list_ths.lu_desc/tag` 聚合；fallback_2: `stock_basic.industry` 聚合 | ⚠ optional+fallback | 框架计算 `sector_strength_source ∈ {limit_cpt_list, lu_desc_aggregation, industry_fallback}` 注入 prompt |
| 人气 | `ths_hot` / `dc_hot` 排名 | `ths_hot/dc_hot` (可选) | ⚠ 视权限 | 缺时跳过 |
| 龙虎榜 | `top_list` 净买入、上榜理由 | `top_list/top_inst` (可选) | ⚠ 视权限 | 缺时跳过 |
| 风险标签 | ST / 停牌（计算） | `stock_st` + `suspend_d` | ✅ | — |
| 缺失声明 | `data_unavailable: list[str]` | 框架计算 | ✅ | — |

> **字段命名**：进入 prompt 前所有字段保持英文 key 不变（便于 LLM 通过 `EvidenceItem.field` 精准引用），数值小数保留 2 位，金额统一以"亿"为单位（封单/市值/成交额）。

#### 12.4.2 Pydantic 响应 Schema（R1）

```python
# limit_up_board/schemas.py
from pydantic import BaseModel, Field, field_validator
from typing import Literal

class EvidenceItem(BaseModel):
    field: str                                 # 必须是输入数据中实际出现的 key（normalized 字段名）
    value: str | int | float | None
    unit: str | None = None                    # 显式单位：'亿'/'万'/'%'/'次'/'日'/'秒'/null
    interpretation: str

    class Config:
        extra = "forbid"

class StrongCandidate(BaseModel):
    candidate_id: str                          # 输入指定的 id，必须原样回传
    ts_code: str
    name: str
    selected: bool
    score: float = Field(ge=0, le=100)
    strength_level: Literal["high", "medium", "low"]
    rationale: str = Field(..., max_length=120)        # 简洁，避免输出膨胀触发 8k/32k 上限截断
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=4)
    risk_flags: list[str] = Field(default_factory=list, max_length=5)
    missing_data: list[str] = []               # 该候选股缺失字段（与全局 data_unavailable 对齐）

    class Config:
        extra = "forbid"

class StrongAnalysisResponse(BaseModel):
    stage: Literal["strong_target_analysis"]
    trade_date: str
    batch_no: int = Field(ge=1)
    batch_total: int = Field(ge=1)
    candidates: list[StrongCandidate]
    batch_summary: str

    class Config:
        extra = "forbid"
```

代码层额外校验（不在 schema 内，由 runner 执行）：

- 输入 `candidate_id` 集合 == 输出 `candidate_id` 集合（不允许遗漏或新增）。
- 任何不在输入集合的 `ts_code` → 视为幻觉 → 该批重试一次（温度不变），仍失败则该批标记 `failed`，事件写 error，但不阻塞其他批次。

#### 12.4.3 强势标的分析 Prompt（R1）

System Prompt：

```text
你是一个 A 股打板策略研究助手。你只能基于本次消息中提供的结构化数据进行分析。

【硬性纪律】
1. 严禁使用外部搜索、新闻网站、公告网站、实时行情、社交媒体、机构观点或任何未提供的数据。
2. 严禁编造新闻、公告、盘口、传闻、龙虎榜席位（除非数据中明确提供）、资金分歧、ETF 申赎流向。
3. 如果某字段缺失（出现在 data_unavailable 中），必须在该候选股的 missing_data 列出，禁止猜测或虚构。
4. 本批次中的每一只候选股都必须出现在 candidates 数组中，且 candidate_id 与输入完全一致。
5. 仅输出 JSON，不要 Markdown 代码块包裹，不要解释性前后缀。

【任务】
对本批次涨停候选股进行"强势标的分析"，判断其是否具备进入下一轮"连板预测"的资格。

【分析维度】
- 封板强度：first_time / last_time / open_times / fd_amount / limit_amount，以及封单与流通市值的比例。
- 板块强度：综合输入【板块强度摘要】section（注意 sector_strength_source 字段，会标注数据来源是 limit_cpt_list / lu_desc 聚合 / industry 聚合，对应可信度递减）。
- 梯队地位：limit_times（连板数）、up_stat（如 "5/7" 表示近 7 日 5 板）。
- 资金确认：当日 net_mf_amount、buy_lg_amount、buy_elg_amount，以及近 3 日趋势。
- 量价结构：pct_chg、amount、turnover_rate、volume_ratio、近 5 日涨跌。
- 流动性与风险：circ_mv、total_mv、open_times、ST/停牌标签、是否一字板、过度连板。

【evidence 要求】
每个候选股至少给出 1 条、至多 4 条 evidence；每条必须引用真实出现在输入中的字段名（field），并填上对应数值（value）、单位（unit）和你的解读（interpretation）。
任何无法用输入字段佐证的 rationale 都视为幻觉。
rationale 不超过 80 字（输出截断会触发 JSON 失败）。

【response schema】
{json_schema}        # 由代码注入 StrongAnalysisResponse 的 JSON Schema

请确保对 batch_no={batch_no} 中的所有 {n} 只候选股逐一给出 StrongCandidate。
```

User Prompt（每批）：

```text
trade_date = {trade_date}
batch_no   = {batch_no}
batch_total= {batch_total}
本批候选股 = {n} 只
全局 data_unavailable = {data_unavailable}    # e.g. ["ths_hot", "dc_hot"]

【市场摘要（仅市场级统计，不含未提交个股的事实）】
{market_summary_json}
   - 全市场涨停 / 跌停 / 炸板数
   - 连板天梯分布（limit_step 全表）
   - 当日 ths_hot / dc_hot TopN（若可用）

【板块强度摘要】
sector_strength_source = {sector_strength_source}     # limit_cpt_list | lu_desc_aggregation | industry_fallback
sector_strength_data   = {sector_strength_json}
   - source=limit_cpt_list 时：含 days/up_nums/cons_nums/rank（最权威）
   - source=lu_desc_aggregation 时：按同花顺涨停原因聚合的当日涨停家数（中等可信）
   - source=industry_fallback 时：按 stock_basic.industry 粗聚合（仅供大类参考）

【本批候选数据】
{candidates_json}      # 每只一条记录，候选间用清晰分隔
   - 每条字段: candidate_id / ts_code / name / industry / lu_desc / tag /
              first_time / last_time / open_times / fd_amount(亿) / limit_amount(亿) /
              limit_times / up_stat / pct_chg / turnover_rate / volume_ratio /
              circ_mv(亿) / total_mv(亿) / net_mf_amount(万) / buy_lg / buy_elg /
              prev5_daily(date,close,pct_chg,vol,amount) /
              prev3_moneyflow(date,net_mf_amount) /
              top_list(若上榜)/ ths_hot_rank / dc_hot_rank
   - 缺失字段在每条候选的 missing_fields 中显式列出

请对所有 {n} 只候选股输出 StrongCandidate，candidate_id 与输入一一对应。
```

### 12.5 LLM 数据需求设计（连板预测 / R2）

#### 12.5.1 候选集

- 第一轮所有 `selected=true` 的强势标的（**严格无截断**）。
- 若 R1 全部未选中 → R2 跳过，输出空结果与原因。

#### 12.5.2 追加输入字段

| 类别 | 字段 | tushare 来源 | 是否可得 |
|---|---|---|---|
| 市场温度 | 当日涨停数、炸板数、跌停数、最高连板高度、连板晋级率、limit_step 完整分布 | `limit_list_d` + `limit_step` | ✅ |
| 板块持续性 | 板块上榜天数、涨停家数变化、板块排名变化（最近 3 日） | primary: `limit_cpt_list` 历史；fallback: `lu_desc` / `industry` 聚合的近 3 日变化 | ⚠ optional+fallback |
| 个股梯队 | 同梯队（同 limit_times）股票数量、是否板块龙头 | 计算自 `limit_list_d` + `sector_strength_*`（按当前 source 计算） | ✅（fallback 下精度递减） |
| 资金延续性 | 近 5 日 `net_mf_amount`、`buy_elg_amount` | `moneyflow` | ✅ |
| 龙虎榜 | 当日净买入、机构净买入 | `top_list` + `top_inst` | ⚠ 可选 |
| 涨停成功率 | `limit_up_suc_rate`（同花顺） | `limit_list_ths` | ⚠ 可选 |
| 次日涨停价 | `up_limit` for T+1（盘前 8:40 后可得） | `stk_limit` | ⚠ 时序 |
| 风险 | 高位加速、连续一字、过度一致、题材孤立 | 计算 | ✅ |

#### 12.5.3 Pydantic 响应 Schema（R2）

```python
class ContinuationCandidate(BaseModel):
    candidate_id: str
    ts_code: str
    name: str
    rank: int = Field(ge=1)
    continuation_score: float = Field(ge=0, le=100)
    confidence: Literal["high", "medium", "low"]
    prediction: Literal["top_candidate", "watchlist", "avoid"]
    rationale: str = Field(..., max_length=200)
    key_evidence: list[EvidenceItem] = Field(min_length=1, max_length=5)
    next_day_watch_points: list[str] = Field(min_length=1, max_length=4)
    failure_triggers: list[str] = Field(min_length=1, max_length=4)
    missing_data: list[str] = []

    class Config:
        extra = "forbid"

class ContinuationResponse(BaseModel):
    stage: Literal["limit_up_continuation_prediction"]
    trade_date: str
    next_trade_date: str
    market_context_summary: str
    risk_disclaimer: str
    candidates: list[ContinuationCandidate]

    @field_validator("candidates")
    @classmethod
    def ranks_unique(cls, v):
        ranks = [c.rank for c in v]
        if len(ranks) != len(set(ranks)):
            raise ValueError("candidate ranks must be unique")
        return v

    class Config:
        extra = "forbid"
```

代码层额外校验：

- 输出 `candidate_id` 集合 == 输入 R1 selected 集合（任意差异 → 重试一次；仍失败 → 整个 R2 标记失败，run.status = `partial_failed`）。
- R2 分批时：`rank` 字段含义切换为"批内本地 rank"（`batch_local_rank`）。
- 单批时：跳过 §12.5.4 的 final_ranking 调用；`rank` 即为最终展示 rank。
- 多批时：必须经过 §12.5.4 的 final_ranking 全局校准，**禁止**裸合并 `continuation_score`。

#### 12.5.4 全局校准（Final Ranking，仅在 R2 触发分批时启用）

**为什么需要**：跨批 `continuation_score` 不可比；某批整体偏强 / 偏弱时，简单合并会产生跨批偏差。因此分批后追加一次 `stage=final_ranking` 调用做全局排序，**严格禁止**新增事实或证据。

**finalists 取样规则**：

- 每批取 `prediction in {top_candidate, watchlist}` 的全部，加 `prediction=avoid` 中 `continuation_score` 最高的若干（保留批边界样本）；
- 单批 finalists 上限 `ceil(batch_size * 0.6)`，避免 finalists 集合本身又超 token。

```python
class FinalRankItem(BaseModel):
    candidate_id: str
    ts_code: str
    final_rank: int = Field(ge=1)
    final_prediction: Literal["top_candidate", "watchlist", "avoid"]
    final_confidence: Literal["high", "medium", "low"]
    reason_vs_peers: str                  # 与同档对比的理由（不允许引入新事实）
    delta_vs_batch: Literal["upgraded", "kept", "downgraded"]   # 相对批内决策的变化

    class Config:
        extra = "forbid"

class FinalRankingResponse(BaseModel):
    stage: Literal["final_ranking"]
    trade_date: str
    next_trade_date: str
    finalists: list[FinalRankItem]

    @field_validator("finalists")
    @classmethod
    def ranks_dense_and_unique(cls, v):
        ranks = sorted(c.final_rank for c in v)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("final_rank must be dense 1..N and unique")
        return v

    class Config:
        extra = "forbid"
```

final_ranking 的 prompt 仅传 finalists 的极简摘要（`candidate_id / ts_code / name / continuation_score / confidence / prediction / rationale[:120] / key_evidence[:3]`）+ §12.5.2 的市场环境摘要；**不**重新提供原始 tushare 字段。

**finalists 范围澄清（评审 §3.4 修复点）**：

- final_ranking **仅对 finalists 排序**，不对全部 R2 候选排序——这是有意设计，避免 finalists 集合本身又超 token。
- **非 finalists 候选**（即各批中未入选 finalists 的 `avoid` 类候选）保留各自批次的 `batch_local_rank` 与原始 LLM 决策；它们**不会丢失**，只是不参与全局重排。
- **最终展示规则**：
  - **Top 候选列表**（`summary.md`、Analysis Panel、Top-K 表格）：来源于 `final_ranking.finalists`，按 `final_rank` 排序。
  - **全量候选明细**（`round2_predictions.json`）：包含**所有** R2 候选（含 finalists 与 non-finalists），每条带 `batch_local_rank`；finalists 额外带 `final_rank` 与 `delta_vs_batch`。
  - 报告中明确标注两者来源差异，避免读者误以为 final_ranking 必须覆盖所有候选。
- 单批 R2 时不触发 final_ranking，所有候选共用同一 `rank` 字段，无 finalists/non-finalists 区分。

#### 12.5.5 连板预测 Prompt（R2）

System Prompt：

```text
你是一个 A 股打板策略研究助手，正在执行第二轮"连板预测"。

【硬性纪律】（与第一轮一致）
1. 严禁使用外部搜索或任何未提供的数据。
2. 严禁编造盘口、龙虎榜席位（除非输入中明确提供）、消息面、传闻、ETF 申赎流向。
3. 输入清单中的每一只标的都必须出现在 candidates 数组中，candidate_id 原样回传。
4. 信息不足时，只能降低 confidence 并在 missing_data 列出缺失字段，禁止猜测。
5. 仅输出 JSON。

【任务】
基于第一轮强势标的及补充数据，预测下一个交易日（next_trade_date）连板可能性，并给出排序。

【判断重点】
- 是否处于主线强势板块（参考输入【板块强度摘要】section；注意 sector_strength_source：limit_cpt_list 最权威，lu_desc_aggregation 中等，industry_fallback 仅供大类参考——可信度递减时应相应降低 confidence）。
- 是否为板块龙头或具备空间板地位（参考 limit_step 全市场最高连板数）。
- 封板质量是否支持次日溢价（fd_amount、open_times、first_time 早 / 晚）。
- 换手是否健康；是否过度一致（一字板）或流动性不足。
- 资金近 5 日是否持续确认（净流入趋势）。
- 龙虎榜（如有）是否显示游资或机构净买入。
- 当前市场连板环境是否支持晋级（高度板存活率、板块发酵阶段）。

【输出语义】
- continuation_score：0-100，模型内部排序分，不等于真实概率。
- confidence：基于证据完整性，缺失字段越多 → confidence 越低。
- prediction：top_candidate（重点关注）/ watchlist（次级关注）/ avoid（回避）。
- next_day_watch_points：次日盘中需要观察的具体指标（如"开盘 20 分钟内是否封板"）。
- failure_triggers：哪些信号出现则放弃（如"开盘后炸板 2 次"）。

【response schema】
{json_schema}
```

User Prompt：

```text
trade_date     = {trade_date}
next_trade_date= {next_trade_date}
本次提交候选数  = {n}            # 来自 R1 selected
data_unavailable = {data_unavailable}

【市场环境】
{market_context_json}
   - 当日全市场涨停/炸板/跌停数
   - limit_step 完整连板分布（如 5板:1, 4板:3, 3板:8, 2板:21, 首板:62）
   - 高度板（连板>=4）家数及次日存活率（基于历史 5 日窗口估算）
   - 同花顺/东方财富热榜 TopN（若可用）

【板块强度摘要】
sector_strength_source = {sector_strength_source}     # limit_cpt_list | lu_desc_aggregation | industry_fallback
sector_strength_data   = {sector_strength_json}       # source=limit_cpt_list 时含 days/up_nums/cons_nums/rank/近3日变化

【候选清单】
{candidates_json}
   - 每条字段: candidate_id / ts_code / name / industry / lu_desc / tag /
              limit_times / up_stat /
              first_time / last_time / open_times / fd_amount(亿) / limit_amount(亿) /
              limit_up_suc_rate /
              same_height_peers（同梯队股数）/ sector_rank / sector_up_nums /
              prev5_moneyflow / top_list / top_inst / next_day_up_limit
   - missing_fields 已显式列出

请对所有 {n} 只输出 ContinuationCandidate。rank 全局唯一且 1..N 连续；若启用本批分批，rank 仅在本批唯一。
```

### 12.6 上下文飘逸的应对策略

> "严禁限制候选股数量"是硬约束。下面是工程级缓解措施，**确保全量覆盖**：

| 风险 | 应对 |
|---|---|
| 候选过多导致单次提交超长 | **按 input + output token 双重预算动态分批**（评审 F5 修复点）：`batch_size = floor(min((input_budget − system − market) / avg_in_per_candidate, (max_output_tokens × safety_ratio) / avg_out_per_candidate))`，`safety_ratio=0.85` 留 15% 安全垫。每只候选都被分配到唯一批次，不存在丢弃。R1 默认 input_budget=80k、max_output=32k；R2 默认 input_budget=200k、max_output=32k。**只看 input 不看 output 会导致 R1 输出截断 → JSON 失败 → partial_failed**。 |
| LLM 长上下文遗漏候选 | 每批输入指定 `candidate_id`；输出强制等长且 id 集合一致；不一致则该批温度 0 重试 1 次。 |
| 字段位置漂移 | 每批 prompt 头部固定字段说明小节；输入用稳定 key 的 JSON 数组而非自然语言。 |
| LLM 编造外部信息 | system 三处明令；evidence 三元组强制引用真实字段；`data_unavailable` 注入告知缺失；Pydantic `extra=forbid` 拒绝多余字段。 |
| 末尾衰减 | system 放硬纪律 + user 末尾再次要求"对所有 {n} 只逐一回复"，首尾双锚。 |
| 数值精度噪声 | 金额/市值统一"亿"，资金"万"，保留 2 位小数，显著缩短 token。 |
| 历史 K 线膨胀 | R1 阶段每只 daily 仅 5 行 × 5 列；moneyflow 仅 3 行；R2 阶段 moneyflow 5 行。 |
| 整段 JSON 失败 | `response_format=json_object` + Pydantic + 重试 1 次仍失败 → 该批标 `failed` 并写 error；其他批继续完成审计，但 run.status = `partial_failed`，最终报告横幅红字告警"结果不完整"（详见 §13）。 |
| 输出长度膨胀 | R1 evidence 上限收紧到 4（原 8）；R1 rationale ≤ 80 字 / R2 ≤ 200 字；max_output_tokens 按 stage 配置（R1/R2 32k、final_ranking 8k，详见 §10.1）。 |
| R1 reduce 阶段 | **v0.1 不引入 R1 reduce**，避免额外信息损耗与 LLM 调用。 |
| R2 跨批分数不可比 | R2 触发分批时**必须**经 §12.5.4 final_ranking 全局校准；**禁止**裸合并 `continuation_score`。单批时跳过校准。 |
| 思维链开销 | DeepSeek V4 思维链占用大量 input tokens；可在 `config set deepseek profile fast` 切到 fast 档（R1/R2 均关 thinking）。 |

> 硬性约束兜底：批次切分逻辑**永不询问用户是否截断**，永不打印"候选数太多"的拒绝信息；只动态扩展批次数。

### 12.7 数据装配代码骨架

```python
# limit_up_board/data.py（节选思路）
def step1_collect_round1(ctx, trade_date, params):
    # 1) 主板池：stock_basic where market='主板' and exchange in ('SSE','SZSE')
    # 2) 当日涨停：limit_list_d where trade_date=T and limit='U'，再 join 主板池
    # 3) 排除 ST：减去 stock_st(T) 名单
    # 4) 排除停牌：suspend_d(T) 中的 ts_code
    # 5) 对剩余每只，确保 daily / daily_basic / moneyflow 近 N 日齐全；缺则补
    # 6) 尝试拉 limit_list_ths（可选）；写入 lub_limit_ths 或标记 unauthorized
    # 7) 计算 lu_desc 衍生 sector tag（同 lu_desc 归一化）
    # 8) 拉市场摘要：limit_step + limit_cpt_list + ths_hot / dc_hot（可选）
    # 9) 返回 (candidates: List[Dict], market_summary: Dict, data_unavailable: List[str])
```

### 12.8 渲染与结果展示

#### 12.8.1 R1 完成后

Analysis Panel：

```markdown
## R1 强势标的（X / Y 选中）

| Code | Name | Score | Level | Theme | 封单(亿) | 连板 | 风险 |
|------|------|-------|-------|-------|----------|------|------|
| ...  | ...  | 87.5  | high  | 人形机器人 | 3.2 | 2板 | 高位加速 |
```

#### 12.8.2 R2 完成后

```markdown
## 次日（{next_trade_date}）连板预测

市场温度: 偏热    主线: 人形机器人 / 算力 / 低空经济

### Top 候选（按 rank）

| # | Code | Name | Score | Confidence | Prediction | Theme | 封单 | 关键证据 | 失败信号 |
|---|------|------|-------|------------|------------|-------|------|----------|----------|

### 风险提示
{risk_disclaimer}
```

#### 12.8.3 报告导出

```text
~/.deeptrade/reports/<run_id>/
├── summary.md                       # Markdown 全报告（适合人看）
├── round1_strong_targets.json       # R1 完整结构化结果（含 batch 维度）
├── round2_predictions.json          # R2 完整结构化结果（含 batch_local_rank）
├── round2_final_ranking.json        # final_ranking 全局校准结果（仅多批时存在）
├── llm_calls.jsonl                  # 每次 LLM 调用一行（含 stage / prompt_hash）
└── data_snapshot.json               # 本次运行使用的市场摘要 + 候选输入快照
```

**报告横幅规则**：

- `partial_failed` / `failed` / `cancelled`：`summary.md` 顶部红色横幅"本次结果不完整，缺失 batches=[...]，不可作为有效筛选结果"。
- `is_intraday=true`（盘中模式）：`summary.md` 顶部黄色横幅 `INTRADAY MODE — 数据可能不完整，仅供盘中观察，不可与日终结果混用`；同时 Live Dashboard 全程 Footer 黄字标记。两者可叠加（顶部双横幅）。

`deeptrade strategy report <run_id>` 命令可在任何时刻重看 + 重新打开看板的 Analysis 面板。

---

## 13. 错误处理 / 限流 / 缓存

### 13.1 行为矩阵

| 场景 | 行为 |
|---|---|
| 未初始化 | 提示运行 `deeptrade init`，可直接跳转交互初始化 |
| Tushare token 无效 | `config test` 失败并提示重新配置；策略 run 前 `validate(ctx)` 自动 ping，失败终止 |
| Tushare 429 | tenacity 退避；连续 3 次后自适应将 `tushare.rps` 减半并持久化 |
| Tushare 5xx | 重试 5 次后**有条件** fallback 到 DB（详见 §13.2 数据新鲜度检查） |
| Tushare 必需接口未授权 | **终止 run（status=failed）** + 提示用户去申请权限 + 给出 doc_id |
| Tushare 可选接口未授权 | `tushare_sync_state.unauthorized` + UI 黄字提示 + prompt 中 `data_unavailable` 注入；按 §11.3 fallback |
| LLM 超时 / 5xx | tenacity 重试；最终失败 → 该批 `validation.failed` 事件 + 该批标 failed |
| LLM JSON 校验失败 | 同 batch 用相同温度再调一次；仍失败 → 该批 failed，事件写 error 与原始响应 |
| LLM 候选 id 集合不一致 | 该批温度 0 重试一次；仍失败 → 该批 failed |
| **任一 batch 最终失败** | run.status = `partial_failed`（**不**伪装成 success）；继续完成其他批以保留审计；report 顶部红色横幅 "结果不完整，不可作为有效筛选结果" |
| **整体运行异常 / 必需接口缺失 / 必需数据 stale** | run.status = `failed` |
| 中断 (Ctrl+C) | 捕获 KeyboardInterrupt → run.status = `cancelled`，写 error_msg，未提交事务回滚 |
| 盘中模式数据隔离 | `--allow-intraday` 模式下，对日终稳定数据写 `data_completeness='intraday'`；`strategy_runs.is_intraday=true`；UI 与 report 顶部强制 `INTRADAY MODE` 黄字横幅；后续日终运行**必定**不命中此缓存（自动重拉为 `'final'`） |
| DuckDB 锁 | 见 §13.3 并发模型 |
| schema 版本不一致 | core schema 与 plugin schema 分别管理；自动 ALTER（仅加列）；危险变更要求 `--allow-migration` |
| 插件 DDL 失败 | 安装事务回滚，清理临时拷贝 |

### 13.2 Tushare fallback 数据新鲜度检查

5xx 重试耗尽后**不无条件**复用本地数据。允许 fallback 必须同时满足：

```python
def can_fallback(api_name, target_T, *, is_intraday_run: bool) -> bool:
    rec = sync_state[(api_name, target_T)]
    if not rec or rec.status != 'ok':       return False
    if rec.trade_date != target_T:          return False    # 不接受相邻日近似
    # row_count=0 是合法情况（如极端市场当日 0 涨停）；不强制下限
    # 真正的"数据缺失"由 status != 'ok' 或 sync 异常时表达，而非用 row_count 推断
    if rec.cache_class == 'trade_day_mutable' and is_T_or_T_plus_1(target_T):
        return False                                          # 修正型数据当日 / 次日不可信
    if not is_intraday_run and rec.data_completeness == 'intraday':
        return False                                          # 日终模式拒绝盘中残缺数据（F4）
    return True
```

任一不满足：

- 必需接口 → 终止 run（`status=failed`），事件写 error；
- 可选接口 → 写 `data_unavailable`，继续。

**空候选合法分支（评审 S4 修复点）**：

- `limit_list_d(T)` 返回 `row_count=0` 且 `status=ok` 是**合法**结果（极端跌停日全市场无涨停）。
- 策略层在 Step 1 数据装配后判断：候选数 = 0 → 直接进入 Step 5 输出"今日无涨停标的"空报告，run.status = `success`，不调用 LLM。
- 不允许把"row_count=0"误判为数据缺失或失败。

### 13.3 DuckDB 并发模型

- **单进程单写连接**：`AppContext` 在 CLI 启动时创建一个 DuckDB 连接，整个进程共享。
- **写串行化**：所有写操作（业务表、`strategy_events`、`llm_calls`、`tushare_calls`）由 runner 主线程统一执行，**不**让并发的 tushare/LLM 调用直接写 DB。
- **并发 I/O**：tushare/LLM 调用本身可异步或线程池并发，但只返回结果到主线程；主线程按事件顺序写入。
- **短事务**：每批 LLM 结果 / 每个接口同步周期作为一个独立事务；不跨步骤 hold 长事务。
- 未来如引入定时同步 daemon，使用任务队列汇聚到主连接，不开多写连接。

---

## 14. 安全与可扩展性

**安全**：

- 插件仅支持本地 path 安装；不内置远程下载。
- 安装前 CLI 显示元数据摘要 + entrypoint + 待创建表 → 用户确认。
- 插件代码通过 `StrategyContext` 受控访问 Tushare / LLM；不接触明文密钥。
- 所有插件运行事件全量记录到 `strategy_events`。

**可扩展（后续 plugin types）**：

- `data_source` — 引入 wind / akshare 等替代/增强源
- `notifier` — 飞书 / 邮件 / Webhook 推送结果
- `backtest` — 回测插件类型
- `report_renderer` — 自定义报告样式

---

## 15. MVP 里程碑

### M1：基础框架（2 人日）
- `deeptrade init` / 目录与库创建 / `schema_migrations`
- `theme.py` + welcome ASCII
- DuckDB 连接封装

### M2：配置与客户端（2 人日）
- `app_config` + `secret_store` + keyring + 降级
- `config show/set/test` 子命令
- `tushare_client`（限流/重试/4 类缓存/审计/fallback 新鲜度）
- `deepseek_client`（JSON + profile 三档 + Pydantic + 永不传 tools）

### M3：插件系统（2 人日）
- `deeptrade_plugin.yaml` 解析 + Pydantic 校验（含 ddl_file / migrations / llm_tools=false 强约束）
- 安装 / 卸载 / disable / enable / info / list / upgrade
- 三阶段分层：install（不联网）/ validate（联网试探）/ run（强校验）
- `plugins` + `plugin_tables` + `plugin_schema_migrations` 持久化

### M4：看板（1.5 人日）
- Live Layout（Header/Progress/Messages/Analysis/Footer）
- StrategyEvent 完整枚举渲染 + `strategy_events` 持久化
- `partial_failed` / `cancelled` 状态横幅渲染

### M5：打板策略 v0.1（5 人日）
- Step 0 算法：`resolve_trade_date()`（默认收盘后 + 18:00 阈值 + `--allow-intraday`）
- 必需接口同步：`stock_basic` / `trade_cal` / `daily` / `daily_basic` / `stock_st` / `limit_list_d` / `limit_step` / `moneyflow`
- 可选接口同步：`limit_list_ths` / `limit_cpt_list` / `top_list` / `top_inst` / `ths_hot` / `dc_hot` / `suspend_d` / `stk_limit`（含 §11.3 fallback）
- R1 prompts + schemas + 分批 + 集合相等校验 + EvidenceItem.unit
- R2 prompts + schemas + 默认单批 + 分批通路（含 batch_local_rank）
- **final_ranking 全局校准**（仅 R2 多批时触发；schema + prompt + 落 `round2_final_ranking.json`）
- `lub_*` 表 + `render_result` + reports/ 导出（5 个文件）

### M6：容错与日志（1 人日）
- tenacity 配置、必需/可选接口分级降级、fallback 新鲜度检查
- `partial_failed` 事件流 + 报告横幅
- `strategy history/report <run_id>` 命令

### M7：文档与样例（1 人日）
- README + 用法 GIF + 插件开发示例文档

**合计 ~14.5 人日 → MVP 可发布**（M1 完成后回顾再调整）

---

## 16. 待澄清问题（已闭环）

| # | 问题 | 用户答复 | 落地方式 |
|---|---|---|---|
| Q1 | DeepSeek 模型名 | `deepseek-v4-pro` 真实存在；1M 上下文，384K 输出；定价见 [pricing 文档](https://api-docs.deepseek.com/zh-cn/quick_start/pricing) | 默认 `model=deepseek-v4-pro`，base_url `https://api.deepseek.com`；阶段级 profile 三档（fast/balanced/quality，默认 balanced）；旧名 `deepseek-chat`/`deepseek-reasoner` 不在默认列表 |
| Q2 | "沪深主板"含义 | 仅主板，不含创/科/北 | `stock_basic.market='主板'` AND `exchange ∈ ('SSE','SZSE')` |
| Q3 | 候选数是否上限 / token 超限是否二次确认 | **必须严格无上限**，**不要**二次确认 | 按 token 预算自动分批；UI 不出现"候选过多"提示；批数无上限 |
| Q4 | 未授权 tushare 接口处理 | 输出提示信息即可 | `tushare_sync_state.unauthorized` + UI 黄字 + prompt 中 `data_unavailable` 注入；`required` 接口缺失才中止 |
| Q5 | "跨日累计"含义 | T 为最近一个交易日（含当日），分析中可使用近 N 交易日历史数据 | Step 0 见 §12.2；R1 daily lookback 默认 10 日，moneyflow 5 日；R2 资金延续性 5 日；不做跨日"接力候选累加" |

### 16.1 已知设计债（v0.4 处理）

第二轮评审中评估为"非阻塞、可后续修"的两项，留档以待后续版本：

| ID | 评审建议 | 当前 v0.3.1 处理 | v0.4 计划 |
|---|---|---|---|
| D1 | `configure()` 改为 schema 驱动：插件只声明 Pydantic 参数 schema，CLI 据此动态生成 questionary 表单；非交互模式可直接传 `--params-file` | v0.3.1 保留 `configure(ctx) -> dict` 简单实现。理由：① v0.1 内置插件仅 `limit-up-board` 一个，UI 风格分裂风险尚不存在；② 改 schema 驱动会牵动 Pydantic↔questionary 转换器、非交互模式、文档示例多处 | 与 plugin_type 扩展（数据源 / 通知 / 回测）一起做，新增 `get_param_schema() -> type[BaseModel]` 与 `get_default_params(ctx) -> BaseModel`，`configure()` 自动派生 |
| D2 | `validate` 阶段对每个 required API 单独 probe：metadata 中声明 `probes:` 字段，按接口 schema 试探最小合法参数 | v0.3.1 仅做"通用连通性自检"：`pro.stock_basic(limit=1)` 探针 + DeepSeek 1-token echo。validate 成功 ≠ 所有接口已可用；真实可用性由 run 阶段首次调用强校验兜底 | 引入 metadata.probes 字段，按 `api_name` 声明 `{trade_date: latest_closed_trade_date, limit: U}` 等最小参数；validate 改为分两层 `validate_connectivity` + `validate_required_apis` |

---

## 18. IM 消息推送机制（v0.4 引入）

### 18.1 设计目标

策略执行完成后，将运行结果推送到外部 IM（飞书 / 钉钉 / 企微 / 自建 webhook 等），便于用户离开终端后获知。三条**强约束**：

1. **CLI 与策略插件严格解耦**：策略插件只把**结构化结果**交给一个统一接口；CLI / 渠道实现负责协议适配和投递。
2. **推送渠道可扩展**：渠道实现以**插件**形态存在（新增 `type=channel`），新增渠道 = 新增插件包，零框架改动。
3. **不阻塞策略执行**：`ctx.notify()` 立刻返回；HTTP 投递在后台 worker 线程进行；CLI 退出前 join 等待清空。

### 18.2 架构总览

```
┌─ Strategy Plugin (run end) ─────────────┐
│   ctx.notify(NotificationPayload(...))  │  ← 插件唯一接触点，立刻返回
└────────────────┬────────────────────────┘
                 │ ctx.notifier (Notifier protocol，由 CLI 注入)
┌────────────────▼────────────────────────┐
│   AsyncDispatchNotifier                 │  ← queue.Queue + daemon worker
│   ├ push(payload) — 入队并立即返回       │
│   └ join(timeout=10) — CLI 退出前调用    │
└────────────────┬────────────────────────┘
                 │
┌────────────────▼────────────────────────┐
│   MultiplexNotifier (fan-out)           │  ← 单通道异常隔离
│   ├─ ChannelPlugin(stdout)   ┐          │
│   ├─ ChannelPlugin(feishu)   │  插件层   │
│   ├─ ChannelPlugin(dingtalk) │           │
│   └─ ChannelPlugin(...)      ┘          │
└─────────────────────────────────────────┘
```

**位置约束**：

- `core/notifier.py` 只含编排逻辑（Notifier/Noop/Multiplex/Async/discovery），不含任何 IM 协议代码
- `plugins_api/notify.py` 定义 `NotificationPayload` 数据契约
- `plugins_api/channel.py` 定义 `ChannelPlugin` Protocol 与 `ChannelContext`
- `channels_builtin/<channel_id>/` 与 `strategies_builtin/` 对称，存放内置渠道实现

### 18.3 数据契约（结构化语义，无渲染）

**设计立场**：插件只传**结构化原始数据**，不传渲染好的 markdown。原因：
- IM 平台格式差异巨大（飞书 markdown / 钉钉 ActionCard / 企微纯文本 / SMS / Slack Blocks），统一 markdown 字符串无法适配
- 插件本身已有结构化结果（如 `predictions`），给报告生成时是 *结构化 → markdown*，给推送应保持 *结构化*
- 渠道决定渲染上限（飞书可做交互卡片、SMS 只能截短文本）

```python
# deeptrade/plugins_api/notify.py
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from deeptrade.core.run_status import RunStatus

class NotificationItem(BaseModel):
    """语义条目；渠道按各自格式渲染（行/卡片/字段）。"""
    model_config = ConfigDict(extra="forbid")
    code: str                      # 显示用主键，如 ts_code
    name: str | None = None
    rank: int | None = None
    score: float | None = None
    label: str | None = None       # e.g. "top_candidate"/"watchlist"
    note: str | None = None        # 短理由
    fields: dict[str, str | int | float] = Field(default_factory=dict)

class NotificationSection(BaseModel):
    """一组语义相关条目（如「次日重点」「观察仓」「回避」）。"""
    model_config = ConfigDict(extra="forbid")
    key: str                       # 稳定标识：top_candidates / watchlist / avoid
    title: str                     # 显示标题
    items: list[NotificationItem]

class NotificationPayload(BaseModel):
    """插件 → Notifier 的全部信息。"""
    model_config = ConfigDict(extra="forbid")
    plugin_id: str
    run_id: str
    status: RunStatus
    title: str                                                    # 单行标题（≤60 字）
    summary: str                                                  # 朴素文本摘要（SMS/邮件主题用）
    sections: list[NotificationSection] = Field(default_factory=list)
    metrics: dict[str, str | int | float] = Field(default_factory=dict)
    report_dir: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)          # 协议外透传
```

`extras` 让插件给特定渠道塞额外字段（如飞书交互卡片的 button URL），普通渠道忽略即可。

### 18.4 ChannelPlugin Protocol

新增第二种 plugin 形态，与 `StrategyPlugin` 平级：

```python
# deeptrade/plugins_api/channel.py
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from deeptrade.core.config import ConfigService
from deeptrade.core.db import Database
from deeptrade.plugins_api.metadata import PluginMetadata
from deeptrade.plugins_api.notify import NotificationPayload

@dataclass
class ChannelContext:
    """渠道插件能看到的能力——比 StrategyContext 更窄。
    没有 tushare、没有 llm、没有 notifier（渠道不会再推到其他渠道）。"""
    db: Database                  # 渠道可声明自己的表（投递日志、幂等键）
    config: ConfigService         # 读取 webhook URL / 密钥
    plugin_id: str | None = None  # 渠道自己的 plugin_id

@runtime_checkable
class ChannelPlugin(Protocol):
    metadata: PluginMetadata
    def validate_static(self, ctx: ChannelContext) -> None: ...
    def validate(self, ctx: ChannelContext) -> None: ...     # 检查必需配置项
    def configure(self, ctx: ChannelContext) -> dict: ...    # 安装时收集 webhook URL/密钥
    def push(self, ctx: ChannelContext, payload: NotificationPayload) -> None:
        """格式转换 + 投递。允许 raise（被 worker 捕获并隔离）。"""
```

**渠道插件特殊豁免**（写入 `docs/plugin-development.md`）：

- 允许直接 `import httpx` / `import requests` 发起 HTTP（策略插件仍禁止）—— 渠道本身就是 HTTP 客户端
- `permissions.tushare_apis` / `permissions.llm` 对渠道无意义，YAML 留空即可
- 渠道**必须**在 `configure(ctx)` 中通过 `ctx.config.set(...)` 收集所需的 webhook URL / token / apikey；密钥走 `secret_store`

### 18.5 编排层（框架内极小集合）

```python
# deeptrade/core/notifier.py — 全部内容仅编排，不含任何 IM 协议
class Notifier(Protocol):
    def is_enabled(self) -> bool: ...
    def push(self, payload: NotificationPayload) -> None: ...

class NoopNotifier:                       # 无渠道时使用
    def is_enabled(self): return False
    def push(self, payload): pass

class MultiplexNotifier:                  # 渠道扇出
    """对每个渠道单独 try/except，单点失败不影响其他渠道。"""
    def push(self, payload):
        for ch, ctx in self._channels:
            try: ch.push(ctx, payload)
            except Exception as e:
                logger.warning("channel %s push failed: %s", ch.metadata.plugin_id, e)

class AsyncDispatchNotifier:              # 异步包装
    """push() 入队立即返回；后台 daemon 线程消费；join() 阻塞等待清空。"""
    def push(self, payload): ...          # queue.put_nowait，队列满则丢弃 + warn
    def join(self, timeout=10.0): ...     # CLI 退出前调用；超时未清空则 warn

def build_notifier(db, plugin_manager) -> Notifier:
    """从已安装且 enabled 的 type=channel 插件列表构建 notifier。"""
    records = [r for r in plugin_manager.list_all() if r.type == "channel" and r.enabled]
    if not records: return NoopNotifier()
    pairs = [(_load_channel_plugin(rec), ChannelContext(db=db, config=ConfigService(db),
              plugin_id=rec.plugin_id)) for rec in records]
    return AsyncDispatchNotifier(MultiplexNotifier(pairs))
```

**异步语义关键点**：

- `push()` 必须立即返回（`queue.put_nowait`）；不向插件暴露任何异步原语
- 队列满（`queue_size=16`）时静默丢弃当前 payload + 一行 warn 日志（HTTP 慢 + 短时间内多次调用的兜底）
- worker 线程是 daemon，主进程退出会被 kill ——所以 CLI **必须**在退出前 `join(timeout=10)`
- 单条投递超时未完成不阻塞 join；`join` 自身在 10s 内退出

### 18.6 StrategyContext 扩展

`StrategyContext` 增加唯一新字段 + 两个便捷方法（最小侵入）：

```python
@dataclass
class StrategyContext:
    # ... 原字段 ...
    notifier: Notifier | None = None        # CLI 注入

    def notify(self, payload: NotificationPayload) -> bool:
        """把 payload 交给 notifier；任何异常都吞掉。
        返回 True 表示已派发到队列；False 表示被禁用或失败。"""
        if self.notifier is None or not self.notifier.is_enabled():
            return False
        try:
            self.notifier.push(payload)
            return True
        except Exception as e:
            logger.warning("notify dispatch failed: %s", e)
            return False

    def is_notify_enabled(self) -> bool:
        """便于插件先判断后构造（避免 IM 关闭时浪费组装富 payload）。"""
        return self.notifier is not None and self.notifier.is_enabled()
```

**插件使用示例**（在 `run()` 末尾）：

```python
if ctx.is_notify_enabled():
    items = [NotificationItem(code=p.ts_code, name=p.name, rank=p.rank,
                              score=p.continuation_score, label=p.prediction,
                              note=p.rationale[:80])
             for p in sorted(predictions, key=lambda x: x.rank)[:10]]
    ctx.notify(NotificationPayload(
        plugin_id="limit-up-board", run_id=ctx.run_id, status=terminal_status,
        title=f"打板 T+1={T1} 入选 {len(selected)} 重点 {n_top}",
        summary=f"{T} 收盘后筛选完成，详见报告：{report_path}",
        sections=[NotificationSection(key="top_candidates", title="次日重点关注",
                                      items=items)],
        report_dir=str(report_path),
    ))
```

### 18.7 CLI 接线

```python
# cli_strategy.py:cmd_run
notifier = build_notifier(db, mgr)
ctx_pre.notifier = notifier
try:
    # ... 原有 validate / configure / runner.execute 链路 ...
finally:
    notifier.join(timeout=10.0)   # ← 关键：阻止 CLI 在异步 push 完成前退出
```

**注意**：失败/取消路径**不做兜底通知**——是否推送完全由策略插件决定。这避免「插件已主动 notify 一次 partial_failed → CLI 又发一次失败通知」的重复打扰，同时保持 CLI 行为对插件透明。

### 18.8 plugin_manager 改动（非破坏性）

- `PluginMetadata.type`：`Literal["strategy"]` → `Literal["strategy", "channel"]`（v0.3.1 安装的策略插件 yaml 仍合法）
- `_load_entrypoint` 返回类型从 `StrategyPlugin` 拓宽为 `StrategyPlugin | ChannelPlugin`
- `validate_static` 调用点根据 `meta.type` 选择 `StrategyContext` 或 `ChannelContext`
- `plugin install / list / info / disable / uninstall / upgrade` 全部命令对两种 plugin 类型同形工作（无需新 CLI 概念）

### 18.9 内置渠道：`channels_builtin/stdout/`

首版内置一个**真实工作但不喧宾夺主**的 stdout 渠道：

- **目的**：① 让 `ctx.notify` 通路在 P0 阶段就能端到端跑通；② 给后续真实渠道作者提供最小参考实现；③ 测试用例的零依赖目标
- **行为**：实际**完整解析** `NotificationPayload`（遍历 sections/items/metrics 触发 Pydantic 校验，写入 stdout 渠道自带的 `stdout_channel_log` 表），但**只在终端输出一行** `✔ push success (run_id=…)`。**绝不**把策略结果重新打印到终端——策略 `render_result` 已经展示过了
- **不把「打印 payload 内容」写到代码里**（即使是为了「调试」），避免诱导其他渠道作者照抄成「也打印一遍」

### 18.10 新增 CLI

仅一个新命令（其余都复用现有 `plugin install/list/disable`）：

```bash
deeptrade channel test [<plugin_id>]    # 给已启用渠道（或指定渠道）派发一条示例 NotificationPayload
```

用途：用户配置完渠道后，在不跑真实策略的情况下验证 webhook URL / 密钥是否正确。

### 18.11 失败处理矩阵

| 场景 | 行为 |
|---|---|
| 用户没装/启用任何渠道 | `NoopNotifier`，`ctx.notify()` 返回 False，零开销 |
| 插件构造 payload 时抛异常 | 不被 `ctx.notify()` 吞——这是插件代码错误，应让 run failed 暴露出来 |
| 单个渠道 `push()` 异常 | `MultiplexNotifier` 捕获并 warn，其他渠道继续 |
| 队列满 | `put_nowait` raise `queue.Full`，捕获后丢弃当前 payload + warn |
| worker 线程因 bug 死掉 | `join` 立即返回（线程不存活）；后续 `push` 仍入队但永不消费——**接受此风险**，daemon 进程退出无副作用 |
| `join(10s)` 超时 | warn 一行，CLI 继续退出（避免某渠道挂死无限阻塞用户） |
| 进程被 SIGKILL | 队列中未发的消息丢失——本工具是短生命周期 CLI，不引入持久化队列 |

### 18.12 与既有约束的兼容

| 既有约束 | 是否受影响 |
|---|---|
| DESIGN §8.5 「插件不直连外部 API」 | 渠道插件**显式豁免**（仅渠道，仍不允许策略插件直连） |
| DESIGN M3「LLM 永不传 tools」 | 完全不受影响，渠道与 LLM 无关 |
| DESIGN §13.1 run status 状态机 | 完全不受影响，notify 是 run 之外的副作用，不进入 status 计算 |
| 现有 plugin install / migrations 流程 | 完全复用；渠道插件走同一套 |
| `permissions.llm_tools=False` 硬约束 | 渠道 yaml 必须保持 False（默认值合法） |

### 18.13 不做的事（明确避免范围蔓延）

- ❌ 服务端长连接 / 消息回执解析 / 双向交互（IM bot 命令）
- ❌ 框架内的模板引擎或 markdown 渲染助手——渠道插件**自治**完成格式转换
- ❌ 持久化重试队列（push 失败即丢，符合「仅通知」语义）
- ❌ asyncio（与同步主链路冲突，不引入）
- ❌ CLI 兜底失败通知（违背「插件全权决定」原则）

---

## 附录 A：Tushare 接口调研来源（已校对）

均直接抓取自 `https://tushare.pro/document/2`（未走互联网搜索）：

| 接口 | doc_id |
|---|---|
| `stock_basic` | 25 |
| `trade_cal` | 26 |
| `daily` | 27 |
| `daily_basic` | 32 |
| `stock_st` | 397 |
| `suspend_d` | 214 |
| `stk_limit` | 183 |
| `limit_list_d` | 298 |
| `limit_list_ths` | 355 |
| `limit_step` | 356 |
| `limit_cpt_list` | 357 |
| `moneyflow` | 170 |
| `moneyflow_ths` | 348 |
| `top_list` | 106 |
| `top_inst` | 107 |
| `ths_hot` | 320 |
| `dc_hot` | 321 |
| `stk_auction_o` | 353 |
| `anns_d` | 176 |
| `news` | 143 |
| `pro_bar` | 109 |
| `ths_index/ths_member/ths_daily` | 259 |

> 字段名、积分门槛与限流以本设计文档表格为准；如官方文档有更新，以官方文档为最终依据。

---

## 附录 B：DeepSeek 资料来源

- Quick Start：`https://api-docs.deepseek.com/`
- Pricing：`https://api-docs.deepseek.com/zh-cn/quick_start/pricing`

关键事实：
- `deepseek-v4-pro` / `deepseek-v4-flash` 为 V4 系列正式模型；`deepseek-chat` / `deepseek-reasoner` 将于 2026-07-24 后弃用，兼容映射到 V4 Flash。
- 1M 上下文，最大输出 384K tokens。
- 支持 JSON Output、Tool Calls、Prompt Caching、Chat Prefix Completion、思维链开关。
- OpenAI 兼容 base URL：`https://api.deepseek.com`。

---

## 附录 C：示例运行（虚拟）

```bash
$ deeptrade init
> Tushare token: ********************
> DeepSeek API Key: ********************
> DeepSeek Model [deepseek-v4-pro]:
✔ Database created: ~/.deeptrade/deeptrade.duckdb
✔ Connectivity check: tushare ✓  deepseek ✓

$ deeptrade plugin install ./strategies_builtin/limit_up_board
─── 即将安装 ─────────────────────────────
plugin_id  : limit-up-board
version    : 0.1.0
type       : strategy
entrypoint : limit_up_board.strategy:LimitUpBoardStrategy
required   : stock_basic, trade_cal, daily, daily_basic, stock_st,
             limit_list_d, limit_step, moneyflow
optional   : limit_list_ths, limit_cpt_list, top_list, top_inst,
             ths_hot, dc_hot, stk_auction_o, anns_d, suspend_d, stk_limit
migrations : 20260427_001 (sha256:abc...)
tables(3)  : lub_limit_list_d, lub_limit_ths, lub_stage_results
（install 阶段不联网；运行前会自动 validate 必需接口可用性）
──────────────────────────────────────────
确认安装? (y/N) y
✔ 已安装。运行 `deeptrade strategy run` 启动。

$ deeptrade strategy run limit-up-board
... (validate ✓ → configure 问卷 → Live Dashboard) ...
✔ Step 0: T=20260427  T+1=20260428  (晚 18:00 后判定为已收盘)
✔ Step 1: 数据补齐完毕（涨停 87 只，data_unavailable=[ths_hot, anns_d]）
◐ Step 2: R1 strong_target_analysis  batch 3/6  (22 candidates)  LLM 12.4s
◐ Step 3: R2 数据增量补齐
◐ Step 4: R2 continuation_prediction  单批 18 候选
○  Step 4.5: 跳过 final_ranking (单批)
✔ Step 5: 报告导出 ~/.deeptrade/reports/01J.../
状态: success

```

---

## 17. 版本修订记录

### v0.3.1 → v0.4（本次）

新增 IM 消息推送机制（详见 §18）。

**新增项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| N1 | 新增 plugin 类型 `channel`：渠道实现以插件形态扩展，框架内 `core/notifier.py` 仅含编排逻辑 | §8 / §18 |
| N2 | 新增 `plugins_api/notify.py`：`NotificationPayload` / `NotificationItem` / `NotificationSection`，纯结构化语义、无 markdown 字段 | §18.3 |
| N3 | 新增 `plugins_api/channel.py`：`ChannelPlugin` Protocol + `ChannelContext`（比 StrategyContext 更窄，无 tushare/llm/notifier） | §18.4 |
| N4 | `core/notifier.py`：`Notifier` Protocol + `NoopNotifier` + `MultiplexNotifier`（fan-out + 单通道异常隔离）+ `AsyncDispatchNotifier`（queue + daemon worker + join(timeout=10)） | §18.5 |
| N5 | `StrategyContext` 新增 `notifier` 字段、`notify(payload)`、`is_notify_enabled()` | §18.6 |
| N6 | `PluginMetadata.type`：`Literal["strategy"]` → `Literal["strategy", "channel"]`（v0.3.1 旧 yaml 仍合法） | §18.4 / §18.8 |
| N7 | 内置 `channels_builtin/stdout/` 参考渠道：完整解析 payload，但终端仅输出一行 `✔ push success` | §18.9 |
| N8 | 新增 CLI `deeptrade channel test [<plugin_id>]` | §18.10 |
| N9 | 渠道插件**显式豁免**「插件不直连外部 API」约束（仅渠道，策略插件仍禁止） | §18.4 |
| N10 | 失败 / 取消路径**不做兜底通知**：是否推送完全由策略插件决定 | §18.7 |

**v0.3.1 → v0.4 概念兼容性**：

- DB schema 增量：渠道插件可声明自己的表（如 `stdout_channel_log`），走现有 `plugin_schema_migrations` 流程，零核心 schema 改动。
- YAML 元数据：`type` 字段放宽，旧 `type: strategy` 完全兼容。
- API 增量：`StrategyContext.notifier` / `notify()` / `is_notify_enabled()` 是新增、非破坏；现有策略不调用 notify 也能正常运行。
- CLI 行为：`deeptrade strategy run` 退出前会等待最长 10 秒清空通知队列；用户没装任何 channel 时 `build_notifier` 返回 `NoopNotifier`、零开销。

### v0.3 → v0.3.1

基于第二轮评审（`E:\personal\DeepTrade1\docs\claude_design_review_round2.md`）的清理修订。

**F（必修）— 5 项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| F1 | `limit_step` 已升 required，从 §11.2 optional 表删除（v0.3 patch 时漏改的残留） | §11.2 |
| F2 | `limit_cpt_list` 多处 `✅` 改为 `optional+fallback`；prompt 引入 `sector_strength_source ∈ {limit_cpt_list, lu_desc_aggregation, industry_fallback}` 标签 | §11.2 / §12.4.1 / §12.4.3 / §12.5.2 / §12.5.5 |
| F3 | fast profile R2 `thinking: true` → `false`，与 §12.6 文案"R1/R2 均关 thinking"一致 | §10.1 |
| F4 | **`--allow-intraday` 缓存污染防护**：`tushare_sync_state` 加 `data_completeness` 列；盘中模式同步日终稳定数据时强制写 `'intraday'`；日终模式严格拒绝命中盘中缓存；UI/report 顶部强制 `INTRADAY MODE` 黄字横幅；`strategy_runs.is_intraday` 字段 | §6.1 / §11.1 / §12.8.3 / §13.1 / §13.2 |
| F5 | **输出 token 截断防护**：`max_output_tokens` 改为 stage 级（R1/R2 默认 32k，final_ranking 8k）；R1 evidence 上限 8→4；R1 rationale ≤ 80 字 / R2 ≤ 200 字；batch_size 同时受 input + output token 双约束 | §10.1 / §10.2 / §12.4.2 / §12.5.3 / §12.6 |

**S（应修）— 5 项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| S1 | **migrations 唯一 DDL 执行源**：`tables` 不再含 `ddl/ddl_file`，仅声明 `name/description/purge_on_uninstall`；首次安装 + 升级共用 migrations；YAML schema 强制 `migrations` 非空 | §8.2 / §8.3 |
| S2 | 18:00 阈值改为 `app.close_after` 配置项；增加"tushare 数据未入库"的明确错误提示 | §7.1 / §12.2 |
| S3 | 移除 `strategy_runs.status` 的 CHECK 约束，改在 Pydantic / Repository 层校验（DuckDB 修改 CHECK 不平滑） | §6.1 |
| S4 | fallback 新鲜度检查移除 `row_count >= 1` 硬编码；row_count=0 是合法结果（极端跌停日）；策略层增加"空候选合法分支"输出空报告而非 failed | §13.2 |
| S5 | final_ranking 范围澄清：finalists 仅是 Top 候选来源；non-finalists 保留 batch_local_rank 入库；`round2_predictions.json` 含全量、`summary.md` 顶部仅展示 finalists | §12.5.4 |

**v0.3 → v0.3.1 概念兼容性**：

- DB schema 增量：`tushare_sync_state` 加 `data_completeness` 列、`strategy_runs` 加 `is_intraday` 列、移除 `status` CHECK 约束 → 通过 ALTER TABLE 加列即可（CHECK 移除需要 DuckDB 建新表+复制方式，由 schema_migrations 处理）。
- YAML 元数据破坏性变更：`tables` 不再支持 `ddl/ddl_file`，**所有插件必须迁移到 `migrations` 段**。v0.3 实验性 yaml 需要 patch（DeepTrade 内置插件随 v0.3.1 一起更新）。
- `DeepSeekClient.__init__` 移除 `max_output_tokens` 参数（改为 stage 级配置）。
- `resolve_trade_date` 新增 `close_after` 必传参数。

### v0.2 → v0.3

基于 ChatGPT 设计评审（`E:\personal\DeepTrade1\docs\claude_design_review.md`）整合的修订。

**M（必改）— 5 项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| M1 | `limit_step` 升级为 `required`；`limit_cpt_list` 保 optional 但加 fallback 算法（lu_desc 聚合 / industry 聚合） | §8.2 / §11.2 / §11.3 |
| M2 | T 日默认改为"最近**已收盘**交易日"（18:00 阈值 + 数据可用性 + `--allow-intraday` 显式开关） | §12.1 / §12.2 / §12.3 |
| M3 | LLM 客户端硬约束：永不传 `tools/tool_choice/functions`；`StrategyContext` 不暴露任何 tool call 接口；`permissions.llm_tools=true` 安装拒绝 | §8.5 / §10.3 |
| M4 | R2 触发分批时增加 `final_ranking` 全局校准调用（FinalRankingResponse schema + finalists 取样规则）；单批跳过 | §12.1 / §12.5.4 / §12.6 |
| M5 | `strategy_runs.status` 扩展 `partial_failed` / `cancelled`；任一 batch 最终失败 → run 标 `partial_failed`；report 顶部红色横幅 | §6.1 / §13.1 / §12.8.3 |

**S（应改）— 5 项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| S1 | DeepSeek 阶段级 profile 配置（fast/balanced/quality 三档；R1 默认不开 thinking，R2 开） | §10.1 / §10.2 / §7.1 |
| S2 | 插件三阶段分层：install（不联网）/ validate（联网试探）/ run（强校验）；`plugin upgrade` 命令 | §8.3 |
| S3 | Tushare 5xx fallback 加数据新鲜度检查（trade_date 完全相等 + row_count 下限 + cache_class 时序约束） | §13.2 |
| S4 | Tushare 缓存按 4 类分层（static / trade_day_immutable / trade_day_mutable / hot_or_anns）；`tushare_sync_state.cache_class` | §6.1 / §11.1 |
| S5 | 插件级 `plugin_schema_migrations` 独立版本管理；YAML 增加 `migrations` 段 + checksum 校验 | §6.1 / §8.2 / §8.3 |

**C（已采纳的可缓项）— 6 项**

| ID | 改动 | 涉及章节 |
|---|---|---|
| C1 | `limit_list_d` 积分文案改为"5000 起可用；8000 解锁更高限流" | §11.2 |
| C2 | "8000 积分刚好覆盖" → "覆盖核心数据，公告/集合竞价为可选增强" | §2.1 |
| C4 | DuckDB 并发模型：单进程单写连接 + 主线程串行写入 | §13.3 |
| C5 | DB 保留 raw 单位，prompt 装配 normalized 字段；EvidenceItem 增加 `unit` 字段 | §10.3 / §11.4 / §12.4.2 |
| C6 | TableSpec 支持 `ddl` 内联或 `ddl_file` 外部引用（二选一） | §8.2 |
| C7 | StrategyEvent 完整枚举（step/data.sync/tushare/llm/validation/result/log） | §8.5 |

**未采纳项（评审 N1-N3）**

| ID | 评审建议 | 不采纳理由 |
|---|---|---|
| N1 | 默认全关 R1 thinking | 用质量优先用户语境为先；改用 S1 profile 切档（默认 balanced 已为 R1 关 thinking、R2 开） |
| N2 | MVP 重估 3-5 周 | 维持 14.5 人日单人估算，M1 完成后回顾再调整 |
| N3 | EVA 主题进一步克制 | v0.2 已注明克制原则，主题色已仅用于边框/状态/强调 |

**v0.2 → v0.3 概念兼容性**：

- 数据库 schema 增量：`strategy_runs.status` CHECK 约束扩展、`tushare_sync_state` 新增 2 列、新增 `plugin_schema_migrations` 表 → 通过 ALTER TABLE 加列 + 软迁移可平滑升级。
- YAML 元数据增量：新增 `migrations` 段、`permissions.llm_tools` 字段；旧 yaml 加载时自动填充默认值（向后兼容）。
- API 增量：`StrategyPlugin` 新增 `validate_static(ctx)`；`StrategyParams` 新增 `allow_intraday`；`DeepSeekClient.complete_json(stage=...)` 必须传 stage（v0.2 已要求）。

### v0.1 → v0.2

- 全面采纳 codex 设计的 20+ 个高价值点（详见对话记录）：`limit_list_ths` / `daily_basic` / `stock_st` / `suspend_d` / `top_list` / 龙虎榜 / 热榜接入；YAML 元数据 + `api_version` + `permissions` 分级；`secret_store` + keyring；`schema_migrations` + `strategy_events` + `tushare_sync_state`；`EvidenceItem(field, value, interpretation)` + `missing_data` + `candidate_id`；report 文件夹导出。
- 删除 v0.1 的 R1 Reduce 阶段。
- 修正 v0.1 错误：T 日为"最近一个交易日（含当天）"而非"上一交易日"。


