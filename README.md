# DeepTrade

> 本地运行的 A 股（沪深主板）选股 CLI 工具：tushare 行情 + OpenAI 兼容 LLM（DeepSeek / Qwen / Kimi …）+ DuckDB 单机仓库 + 插件式 CLI 框架。

> 📖 **在线文档**：[deeptrade.tiey.ai](https://deeptrade.tiey.ai) — 用户手册 + 开发者手册 + 官方插件目录

[![tests](https://img.shields.io/badge/tests-passing-brightgreen)](#) [![python](https://img.shields.io/badge/python-3.11+-blue)](#) [![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## ✨ 主要特性

- **轻量本地化**：单文件 DuckDB + uv，一条命令跑完，无需服务进程或容器。
- **纯透传式插件 CLI**：框架命令面**封闭**——只管 `init / config / plugin / data`；其余命令一律按 `deeptrade <plugin_id> <argv...>` 透传给插件，插件自管 `--help`、子命令、参数、持久化。新增插件类型（皮肤、新数据源、回测、IM 渠道……）零框架改动。
- **数据隔离（Plan A）**：每个插件在自己的 migrations 里声明并拥有自己的表（含 tushare 派生数据），框架不持有任何业务表。`tushare_sync_state` / `tushare_calls` / `llm_calls` 都按 `plugin_id` 维度隔离。
- **顶层通知 API**：`from deeptrade import notify, notification_session` 一行发推送；框架根据已安装的 channel 插件自动路由，无 channel 时自动 noop。
- **多 LLM Provider 共存**：`llm.providers` 字典化配置，多个 OpenAI 兼容服务并存；插件通过 `LLMManager.get_client(name=...)` 按名取用，单次 run 内可调多家。
- **LLM 强约束**：JSON 模式 + Pydantic 双层校验；**永远**不传 tools / function calls。
- **盘中数据隔离**：`--allow-intraday` 模式下同步的不完整数据写入 `data_completeness='intraday'`，日终模式严格拒绝命中。

## 🚀 5 分钟上手

### 安装

```bash
# 推荐：pipx 隔离环境（命令名仍是 deeptrade）
pipx install deeptrade-quant
# 或
uv tool install deeptrade-quant
```

> **注**：PyPI 项目名是 `deeptrade-quant`，CLI 命令是 `deeptrade`，Python 包名是 `deeptrade`（`import deeptrade`）。三者不同是 Python 生态常态（同 `pip install scikit-learn` → `import sklearn`）。

```bash
# 开发模式（克隆本仓库，editable install）
uv sync --all-extras
uv run pre-commit install
# 兜底（无 uv）
python -m venv .venv && source .venv/bin/activate  # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"
```

### 初始化与配置

```bash
deeptrade init                            # 建库 + 应用 core migrations
deeptrade config set-tushare              # 交互式输入 tushare token
deeptrade config set-llm                  # 交互式增/改/删 LLM provider（deepseek / qwen / kimi …）
deeptrade config list-llm                 # 列出已配置且可用的 provider
deeptrade config test-llm                 # 对所有 provider 做连通性自检（也可加 <name> 单测）
deeptrade config show                     # 表格展示当前配置（密钥脱敏）
```

### 安装官方插件并运行

官方插件维护在 [DeepTradePluginOfficial](https://github.com/ty19880929/DeepTradePluginOfficial)，框架通过短名查注册表 → 拉 GitHub release tarball 自动安装。

```bash
# 浏览注册表
deeptrade plugin search

# 按短名安装（注册表 → 最新 release tag）
deeptrade plugin install limit-up-board
deeptrade plugin install volume-anomaly
deeptrade plugin install stdout-channel

deeptrade plugin list                     # 查看已安装

# 运行打板策略（CLI 由插件自管，--help 由插件渲染）
deeptrade limit-up-board --help
deeptrade limit-up-board run              # 默认日终模式
deeptrade limit-up-board run --allow-intraday --force-sync

# 运行成交量异动策略（三模式）
deeptrade volume-anomaly screen           # 异动筛选 → upsert va_watchlist
deeptrade volume-anomaly analyze          # LLM 主升浪启动预测
deeptrade volume-anomaly prune --days 30  # 剔除追踪 ≥30 日的标的

# 推送链路自检
deeptrade stdout-channel test
```

> **第三方插件 / 本地开发**：`deeptrade plugin install ./path/to/my-plugin` 仍可装本地目录；`deeptrade plugin install https://github.com/owner/repo` 装完整 git 仓库（仓库根需含 `deeptrade_plugin.yaml`）。详见 [`docs/plugin-development.md`](docs/plugin-development.md)。

报告产出在 `~/.deeptrade/reports/<run_id>/`。

## 📦 命令矩阵

### 框架命令（封闭集合）

| 命令 | 用途 |
|---|---|
| `deeptrade --version` / `-V` | 显示版本 |
| `deeptrade --help` / `-h` | 框架命令清单（**不**枚举插件子命令） |
| `deeptrade init [--no-prompts]` | 建库 + 应用 core migrations |
| `deeptrade config {show, set, set-tushare, set-llm, list-llm, test-llm}` | 全局配置 |
| `deeptrade plugin install <path> [-y]` | 本地路径安装（绝不联网） |
| `deeptrade plugin list / info <id>` | 列表 / 详情 |
| `deeptrade plugin enable <id> / disable <id>` | 启 / 停 |
| `deeptrade plugin uninstall <id> [--purge]` | 卸载（`--purge` 才 DROP 表） |
| `deeptrade plugin upgrade <path>` | 升级（增量 migrations） |
| `deeptrade data sync ...` | （暂停用，下版本恢复） |

保留字（不可作为 plugin_id）：`init / config / plugin / data`。

### 插件命令（按 plugin_id 透传，插件自管）

| 命令 | 来源 |
|---|---|
| `deeptrade limit-up-board {run, sync, history, report}` | 内建打板策略插件 |
| `deeptrade volume-anomaly {screen, analyze, prune, history, report}` | 内建成交量异动插件 |
| `deeptrade stdout-channel {test, log}` | 内建 stdout 通知插件 |
| `deeptrade <你的-plugin-id> ...` | 你自己写的任何插件 |

任意插件子命令的 `--help` 都由插件自身渲染——框架不感知动词语义。

## 🧱 架构

```
┌──────────────────────── deeptrade CLI (custom click.Group) ────────────────────────┐
│                                                                                    │
│  framework commands (closed):                                                      │
│      init │ config │ plugin │ data                                                 │
│                                                                                    │
│  plugin pass-through (open):                                                       │
│      <plugin_id>  ──argv──→  Plugin.dispatch(argv) → int  (plugin owns the rest)   │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │
            ┌──────────────────────┼──────────────────────────┐
            ▼                      ▼                          ▼
   ┌─────────────────┐   ┌──────────────────┐    ┌────────────────────┐
   │ Core services    │   │ deeptrade.notify │    │  Plugins (any type)│
   │ DuckDB · Config │   │ → routes to all  │    │  • metadata        │
   │ Tushare · LLM   │   │   enabled        │    │  • validate_static │
   │ Notifier        │   │   channel plugins│    │  • dispatch(argv)  │
   └─────────────────┘   └──────────────────┘    │  (channel: + push) │
                                                  └────────────────────┘
```

设计决策见 [docs/plugin_cli_dispatch_evaluation.md](docs/plugin_cli_dispatch_evaluation.md)。

## 📖 文档

- [DESIGN.md](DESIGN.md) — 设计文档
- [docs/plugin_cli_dispatch_evaluation.md](docs/plugin_cli_dispatch_evaluation.md) — 当前架构的评估与决策记录（v0.3）
- [docs/quick-start.md](docs/quick-start.md) — 上手指南
- [docs/plugin-development.md](docs/plugin-development.md) — 写一个新插件
- [docs/limit-up-board.md](docs/limit-up-board.md) — 打板策略说明
- [CHANGELOG.md](CHANGELOG.md) — 版本变更

## ⚖️ 免责声明

本工具仅用于策略研究、数据整理与候选标的分析，**不构成投资建议**，**不进行自动交易**。所有 LLM 输出基于提交的结构化数据，不引用任何外部信息源；用户应自行核验候选标的的最新状态后再做决策。
