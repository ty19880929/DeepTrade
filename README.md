# DeepTrade

> 本地运行的 A 股（沪深主板）选股 CLI 框架：tushare 行情 + OpenAI 兼容 LLM（DeepSeek / Qwen / Kimi …）+ DuckDB 单机仓库 + 纯透传式插件 CLI。框架不携带任何业务策略，所有策略按需从官方注册表安装。

> 📖 **在线文档**：[deeptrade.tiey.ai](https://deeptrade.tiey.ai) — 用户手册 + 开发者手册 + 官方插件目录

[![tests](https://img.shields.io/badge/tests-passing-brightgreen)](#) [![python](https://img.shields.io/badge/python-3.11+-blue)](#) [![license](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![version](https://img.shields.io/badge/version-0.3.0-blue)](CHANGELOG.md)

## ✨ 主要特性

- **轻量本地化**：单文件 DuckDB + uv，一条命令跑完，无需服务进程或容器。
- **框架与插件物理解耦**：`deeptrade-quant` wheel 只含框架（`init / config / plugin / data`），所有策略走 `deeptrade plugin install <短名>` 从注册表安装。
- **纯透传式插件 CLI**：未知首词一律按 `deeptrade <plugin_id> <argv...>` 透传给插件，插件自管 `--help`、子命令、参数、持久化。
- **数据隔离（Plan A）**：每个插件在自己的 migrations 里声明并拥有自己的表（含 tushare 派生数据），框架不持有任何业务表。`tushare_sync_state` / `tushare_calls` / `llm_calls` 都按 `plugin_id` 维度隔离。
- **多 LLM Provider 共存**：`llm.providers` 字典化配置，多个 OpenAI 兼容服务并存；插件通过 `LLMManager.get_client(name=...)` 按名取用，单次 run 内可调多家。
- **LLM 强约束**：JSON 模式 + Pydantic 双层校验；**永远**不传 tools / function calls。

## 🚀 5 分钟上手

### 安装框架

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
deeptrade init                            # 建库 + 应用 core migrations（交互式可选配 tushare / LLM）
deeptrade config set-tushare              # 交互式输入 tushare token
deeptrade config set-llm                  # 交互式增/改/删 LLM provider（deepseek / qwen / kimi …）
deeptrade config list-llm                 # 列出已配置且可用的 provider
deeptrade config test-llm                 # 对所有 provider 做连通性自检（也可加 <name> 单测）
deeptrade config show                     # 表格展示当前配置（密钥脱敏）
```

### 安装官方插件并运行

官方插件维护在 [DeepTradePluginOfficial](https://github.com/ty19880929/DeepTradePluginOfficial)，框架按短名查注册表 → 拉 GitHub release tarball 自动安装。

```bash
# 浏览注册表
deeptrade plugin search                   # 全量列出
deeptrade plugin search anomaly           # 关键词过滤

# 按短名安装（注册表 → 该插件最新 release tag）
deeptrade plugin install limit-up-board
deeptrade plugin install volume-anomaly

deeptrade plugin list                     # 查看已安装

# 运行打板策略（CLI 由插件自管，--help 由插件渲染）
deeptrade limit-up-board --help
deeptrade limit-up-board run              # 默认日终模式
deeptrade limit-up-board run --allow-intraday --force-sync

# 运行成交量异动策略
deeptrade volume-anomaly screen           # 异动筛选 → upsert va_watchlist
deeptrade volume-anomaly analyze          # LLM 主升浪启动预测
deeptrade volume-anomaly evaluate         # T+N 自动回测闭环
deeptrade volume-anomaly stats            # 收益统计聚合
```

> **第三方 / 本地开发插件**：`deeptrade plugin install <SOURCE>` 三种来源统一处理，判定顺序为 *本地目录存在 → git URL → 注册表短名*：
>
> - `deeptrade plugin install ./path/to/my-plugin` — 本地目录
> - `deeptrade plugin install https://github.com/owner/repo` — 完整 git 仓库（仓库根需含 `deeptrade_plugin.yaml`）
> - `deeptrade plugin install my-plugin --ref v1.2.0` — 指定 tag/branch/sha

报告产出在 `~/.deeptrade/reports/<run_id>/`。

## 📦 命令矩阵

### 框架命令（封闭集合）

| 命令 | 用途 |
|---|---|
| `deeptrade --version` / `-V` | 显示版本 |
| `deeptrade --help` / `-h` | 框架命令清单（**不**枚举插件子命令） |
| `deeptrade init [--no-prompts]` | 建库 + 应用 core migrations |
| `deeptrade db init` / `db upgrade` | 显式建库 / 应用待执行迁移 |
| `deeptrade config {show, set, set-tushare, set-llm, list-llm, test-llm}` | 全局配置 |
| `deeptrade plugin search [keyword] [--no-cache]` | 浏览官方注册表 |
| `deeptrade plugin install <SOURCE> [--ref <REF>] [-y]` | 注册表短名 / GitHub URL / 本地路径 |
| `deeptrade plugin list` / `info <id>` | 列表 / 详情（未安装时回退注册表条目） |
| `deeptrade plugin enable <id>` / `disable <id>` | 启 / 停 |
| `deeptrade plugin uninstall <id> [--purge]` | 卸载（`--purge` 才 DROP 表） |
| `deeptrade plugin upgrade <SOURCE> [--ref <REF>]` | 升级（SemVer 比较，禁止降级；增量 migrations） |
| `deeptrade data sync ...` | （暂停用，下版本恢复；改用插件自带的 sync 子命令） |

保留字（不可作为 plugin_id）：`init / config / plugin / data / db`。

### 插件命令（按 plugin_id 透传，插件自管）

| 命令 | 来源（注册表短名） |
|---|---|
| `deeptrade limit-up-board {run, sync, history, report, settings}` | `limit-up-board`（strategy） |
| `deeptrade volume-anomaly {screen, analyze, evaluate, stats, prune, history, report}` | `volume-anomaly`（strategy） |
| `deeptrade <你的-plugin-id> ...` | 你自己写的任何插件 |

任意插件子命令的 `--help` 都由插件自身渲染——框架不感知动词语义。各插件的最新子命令、参数与运行手册见 [DeepTradePluginOfficial](https://github.com/ty19880929/DeepTradePluginOfficial)。

## 🧱 架构

```
┌──────────────────────── deeptrade CLI (custom click.Group) ────────────────────────┐
│                                                                                    │
│  framework commands (closed):                                                      │
│      init │ config │ plugin │ data │ db                                            │
│                                                                                    │
│  plugin pass-through (open):                                                       │
│      <plugin_id>  ──argv──→  Plugin.dispatch(argv) → int  (plugin owns the rest)   │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │
                                   │
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
        ┌──────────────────┐              ┌────────────────────┐
        │ Core services    │              │  Plugins (strategy)│
        │ DuckDB · Config  │              │  • metadata        │
        │ Tushare · LLM    │              │  • validate_static │
        └──────────────────┘              │  • dispatch(argv)  │
                                          └────────────────────┘
```

每个插件通过自己的 migrations 声明并拥有 `<prefix>_*` 业务表；`tushare_sync_state` / `tushare_calls` / `llm_calls` 按 `plugin_id` 维度隔离，`__framework__` 为框架自身保留 sentinel。

## 📖 参考

- [CHANGELOG.md](CHANGELOG.md) — 版本变更与历次 breaking change 记录
- [DeepTradePluginOfficial](https://github.com/ty19880929/DeepTradePluginOfficial) — 官方插件源码、注册表、各插件运行手册
- 历史快照 `archive/with-builtin-plugins-v0.1.0-preview` — 含 builtin 子树的最后一版状态（v0.2.0 之前）

## ⚖️ 免责声明

本工具仅用于策略研究、数据整理与候选标的分析，**不构成投资建议**，**不进行自动交易**。所有 LLM 输出基于提交的结构化数据，不引用任何外部信息源；用户应自行核验候选标的的最新状态后再做决策。
