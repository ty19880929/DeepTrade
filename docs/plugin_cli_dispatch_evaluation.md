# 插件化 CLI 调度模型 · 评估报告 v0.3

> 输入议题：
> 1. 框架不应再承担"按插件类型动词"（`strategy run`、`channel test` 等）的命令组——插件类型扩展后会出现"皮肤""IM 渠道""数据源"等不同语义动词，框架枚举不可持续。
> 2. 框架侧只保留三件事：① 全局配置（tushare token、LLM apikey 等）② 插件安装/注册/卸载 ③ 形如 `deeptrade <plugin_name> xxx --yyy` 的**纯透传**式调度，由插件自己解析参数与处理动作。
> 3. **本工具尚处于开发迭代和试用阶段，不考虑任何兼容性方案。**
> 4. 框架与插件之间的 schema 边界要清晰：框架 migrations 只管框架自己的表，插件 migrations 只管插件自己的表，互不越界。
>
> 文档版本：v0.3 · 2026-04-30 · 设计阶段（v0.2 + 第二轮 5 项决策 + migration 架构修订）

---

## 0. 用户决策摘要（截至 v0.3）

### 第一轮决策（v0.2 已采纳）

| # | 决策点 | 裁决 |
|---|---|---|
| 1 | CLI 实现方案 | **方案 C — 纯透传**。框架只切 `<plugin_name>`，剩余 argv 整体丢给插件 `dispatch(argv)`；框架仅保证自己的 `--help` 正确，插件 `--help` 由插件自管 |
| 2 | `strategy_runs` 归宿 | **(c) 删除框架持久化**。各插件自管 history / report；插件之间不要为了"复用"而耦合，即便有重复也接受 |
| 3 | 公共开关复用 | **不提供**。`--force-sync` / `--no-dashboard` / `--allow-intraday` 不通用，全部交插件自管 |
| 4 | `data sync` | **保持现状**，本版本不动，下一版本再处理 |
| 5 | 兼容期长度 | **不考虑兼容性**——直接破坏式重构 |
| 6 | 同名 CLI / 顶层 namespace | **不需要 namespace 抽象**，插件命令各自为政、互不影响 |
| 7 | 保留字 | **仅与框架级命令不冲突**即可；当前无额外保留字 |

### 第二轮决策（v0.3 新增）

| # | 决策点 | 裁决 |
|---|---|---|
| 8 | `deeptrade hello` | **删除** |
| 9 | 交互式主菜单 | **直接移除** |
| 10 | `PluginMetadata.type` 字段 | **保留**为元信息（`plugin list` 可见分类，框架行为不依赖） |
| 11 | `ChannelPlugin` Protocol / notifier | **框架保留"推送消息"能力并兜底**：插件调用统一的 `notify(payload)` 接口，框架根据已安装的 channel 插件路由；无 channel 时为空实现（NoopNotifier）。`ChannelPlugin` Protocol 保留为 channel 插件被框架调用的契约 |
| 12 | 实施计划 | 同意 v0.2 §6 的 8 步顺序 |
| 13 | Migration 架构 | **框架 migrations 与插件 migrations 完全隔离**。任何被插件读/写的表都不应在框架 migrations 里出现；框架只管自己用的表（详见 §6） |
| 14 | 5 张共享市场表的处置 | **方案 A — 纯隔离**。本地工具、不会同时跑多套策略、磁盘视为无限。5 张表从 core migrations 删除，TushareClient 退化为纯调用（不再写命名业务表），每个插件自己声明并持有需要的 tushare 派生表 |

---

## 1. 现状：完整命令矩阵

下列为 v0.4 当前**所有已实现**的命令、选项与作用，按用户要求列全，便于和你预期的命令矩阵对比。

### 1.1 顶层

| 命令 | 选项 | 作用 |
|---|---|---|
| `deeptrade` (无参数 + TTY) | — | 进入交互式主菜单（7 项硬编码：init / set-tushare / set-deepseek / plugin list / strategy run / strategy history / exit） |
| `deeptrade --version` / `-V` | — | 显示版本号后退出 |
| `deeptrade --help` / `-h` | — | typer 自动生成 |
| `deeptrade hello` | — | smoke test，输出 NERV 风格问候 |
| `deeptrade init` | `--no-prompts` | 创建 `~/.deeptrade` 目录布局 + 应用 core 迁移；幂等。`--no-prompts`：跳过安装后的 tushare/deepseek 交互配置追问 |

### 1.2 `deeptrade config` — 全局配置

| 命令 | 参数 / 选项 | 作用 |
|---|---|---|
| `config show` | — | 列出所有已知 key 的当前值（密钥脱敏）与来源（default / db / env） |
| `config set <key> <value>` | `key`：点分 key，例 `deepseek.profile`；`value`：字符串值 | 单 key 写入（脚本化用） |
| `config set-tushare` | — | 交互式配置 `tushare.token` / `tushare.rps` / `tushare.timeout` |
| `config set-deepseek` | — | 交互式配置 `deepseek.api_key` / `deepseek.base_url` / `deepseek.model` / `deepseek.profile`（fast/balanced/quality） |
| `config test` | — | 端到端联通测试：分别走 TushareClient 调一次 `stock_basic`、走 DeepSeekClient 用 final_ranking profile 跑一次 1-token JSON echo |

### 1.3 `deeptrade plugin` — 插件生命周期

| 命令 | 参数 / 选项 | 作用 |
|---|---|---|
| `plugin install <path>` | `path`：本地插件目录；`-y` / `--yes`：跳过确认 | 解析 `deeptrade_plugin.yaml` → 校验 → 摘要预览 → 落 `plugins` 表 + 应用插件迁移 |
| `plugin list` | — | 表格显示所有已安装插件（plugin_id / name / version / enabled） |
| `plugin info <plugin_id>` | `plugin_id` | dump 该插件的完整 metadata YAML |
| `plugin enable <plugin_id>` | `plugin_id` | 启用 |
| `plugin disable <plugin_id>` | `plugin_id` | 禁用 |
| `plugin uninstall <plugin_id>` | `plugin_id`；`--purge`：DROP 插件表并丢弃所有数据；`-y` / `--yes`：跳过确认 | 默认仅禁用 + 保留表；`--purge` 才删表 |
| `plugin upgrade <path>` | `path`：本地插件目录 | 升级到新版本（应用增量迁移） |

### 1.4 `deeptrade strategy` — strategy 类型专属（**重构后删除**）

| 命令 | 参数 / 选项 | 作用 |
|---|---|---|
| `strategy list` | — | 仅列出 type=strategy 且 enabled 的插件 |
| `strategy run [plugin_id]` | `plugin_id`：可省，TTY 时交互选择；`--trade-date YYYYMMDD`：指定交易日；`--allow-intraday`：允许盘中模式；`--force-sync`：强制重新拉数据；`--no-dashboard`：禁用 Live/TUI 看板（管道/CI 用） | 串行调用 plugin.validate() → configure() → run()；写 `strategy_runs` 表；按需启动 TUI；run 完调用 plugin.render_result() 打印简报；最后通过 notifier 推 channel 插件 |
| `strategy history` | `--limit N`（默认 20） | 从 `strategy_runs` 表查最近 N 条 run（run_id / plugin_id / trade_date / status / 时间戳） |
| `strategy report <run_id>` | `run_id`；`--full`：dump 完整 markdown summary | 默认调插件 `render_result` 重渲简报；`--full` 直出 `~/.deeptrade/reports/<run_id>/summary.md` |

### 1.5 `deeptrade data` — 数据同步（**保持现状，下版本处理**）

| 命令 | 参数 / 选项 | 作用 |
|---|---|---|
| `data sync [plugin_id]` | `plugin_id`：可省，TTY 时交互选择；`--trade-date YYYYMMDD`；`--allow-intraday`；`--force-sync` | 调用 plugin.sync_data()——只拉数+落库，跳过 LLM stages |

### 1.6 `deeptrade channel` — channel 类型专属（**重构后删除**）

| 命令 | 参数 / 选项 | 作用 |
|---|---|---|
| `channel list` | — | 仅列出 type=channel 的插件（已被 `plugin list --type=channel` 覆盖，但目前没有 `--type` 过滤选项） |
| `channel test [plugin_id]` | `plugin_id`：可省，省则向所有 enabled channel 派发 | 合成一份 `NotificationPayload`（含示例条目）→ 通过 notifier 推送 → 阻塞等队列 drain → 报结果 |

### 1.7 一些"看不见的入口"（值得列出，避免重构遗漏）

- 交互式主菜单 `_interactive_main_menu`：硬编码 7 项，其中第 4/5/6 项调用 `cli_plugin.cmd_list` / `cli_strategy.cmd_run` / `cli_strategy.cmd_history`。重构后菜单需要重做：要么撤销，要么改为枚举所有已启用插件让用户选。
- `cli_data.cmd_sync` 复用了 `cli_strategy._pick_strategy_interactively` 做插件选择——`strategy` 模块被删除前需先把这个 helper 提取出来。

---

## 2. 目标：重构后命令矩阵

### 2.1 框架命令（**完整且封闭**）

| 命令 | 选项 | 作用 | 备注 |
|---|---|---|---|
| `deeptrade --version` / `-V` | — | 版本号 | 不变 |
| `deeptrade --help` / `-h` | — | 仅展示框架级命令 + 提示"已安装插件请用 `plugin list` 查看，调用方式 `deeptrade <plugin_id> --help`" | **不**枚举插件子命令 |
| `deeptrade init` | `--no-prompts` | 同现状 | 不变 |
| `deeptrade config show` | — | 同现状 | 不变 |
| `deeptrade config set <key> <value>` | — | 同现状 | 不变 |
| `deeptrade config set-tushare` | — | 同现状 | 不变 |
| `deeptrade config set-deepseek` | — | 同现状 | 不变 |
| `deeptrade config test` | — | 同现状 | 不变 |
| `deeptrade plugin install <path>` | `-y/--yes` | 同现状，**新增**保留字校验 | 详见 §3.4 |
| `deeptrade plugin list` | — | 同现状 | 不变 |
| `deeptrade plugin info <plugin_id>` | — | 同现状 | 不变 |
| `deeptrade plugin enable <plugin_id>` | — | 同现状 | 不变 |
| `deeptrade plugin disable <plugin_id>` | — | 同现状 | 不变 |
| `deeptrade plugin uninstall <plugin_id>` | `--purge`, `-y/--yes` | 同现状 | 不变 |
| `deeptrade plugin upgrade <path>` | — | 同现状 | 不变 |
| `deeptrade data sync [plugin_id]` | `--trade-date`, `--allow-intraday`, `--force-sync` | 同现状 | **保留，下版本处理** |
| `deeptrade <plugin_id> ...argv` | 由插件解析 | 框架仅按 plugin_id 路由，剩余 argv 整体透传给 `plugin.dispatch(argv)` | **核心新增** |

**已敲定**：
- `deeptrade hello` → 删除（决策 #8）
- `deeptrade`（无参数 + TTY）的交互菜单 → 直接移除（决策 #9）。`deeptrade` 无参数等价于 `deeptrade --help`

### 2.2 插件命令

**框架不感知。**示例（仅作举例，重构后由插件自己负责）：

```
deeptrade limit-up-board run --trade-date 20260430 --force-sync
deeptrade limit-up-board history --limit 50
deeptrade limit-up-board report <run_id> --full
deeptrade limit-up-board sync --trade-date 20260430

deeptrade volume-anomaly screen
deeptrade volume-anomaly analyze --mode pro
deeptrade volume-anomaly prune --days 7

deeptrade feishu test           # 假设的 channel 自测
deeptrade feishu push --title "x" --body "y"

deeptrade nerv-skin apply       # 假设的皮肤插件
```

每个插件的命令集、选项、行为、历史记录、报告渲染**全部归插件本身**，框架完全不知道。

---

## 3. 核心设计：纯透传调度

### 3.1 路由规则

伪代码（实际用 click.Group 自定义比 typer 的多 group 结构更顺手）：

```python
def main(argv: list[str]) -> int:
    if not argv:
        print_framework_help()
        return 0

    first = argv[0]

    # 1) 框架级保留字 / 全局选项
    if first in {"--version", "-V"}:
        print(f"DeepTrade {__version__}"); return 0
    if first in {"--help", "-h"}:
        print_framework_help(); return 0
    if first in FRAMEWORK_COMMANDS:           # init / config / plugin / data
        return FRAMEWORK_COMMANDS[first](argv[1:])

    # 2) 插件路由
    rec = plugin_registry.lookup(first)
    if rec is None:
        print(f"✘ unknown command or plugin: {first!r}")
        print(f"  framework commands: {sorted(FRAMEWORK_COMMANDS)}")
        print(f"  use `deeptrade plugin list` to see installed plugins")
        return 2
    if not rec.enabled:
        print(f"✘ plugin {first!r} is disabled; run `deeptrade plugin enable {first}`")
        return 2

    plugin = load_entrypoint(rec)
    if not hasattr(plugin, "dispatch"):
        print(f"✘ plugin {first!r} does not implement dispatch()")
        return 2

    return plugin.dispatch(argv[1:])           # 纯透传剩余 argv
```

要点：
- 框架命令优先；落不到框架命令再去插件表里查。
- 不维护"插件命令树"——框架既不知道插件有哪些子命令，也不知道插件接受什么选项。
- 插件 `dispatch(argv: list[str]) -> int` 返回退出码（0=成功，非 0=失败，与 typer/click 约定一致）。
- 出错信息在框架侧只能给到"未知命令/插件"层级；具体到"未知子命令""错误选项"由插件自己抛。

### 3.2 插件契约变更

现有 `StrategyPlugin` Protocol 的所有钩子（`validate / configure / run / render_result / wants_dashboard / sync_data / validate_static`）都是**框架在调用**——重构后框架不再知道这些。

新的 Plugin 契约（最小化）：

```python
@runtime_checkable
class Plugin(Protocol):
    metadata: PluginMetadata

    def validate_static(self, ctx) -> None:
        """安装后自检；不允许网络。框架在 `plugin install` 时调用。"""

    def dispatch(self, argv: list[str]) -> int:
        """命令分发入口。argv 是去掉 plugin_id 后的剩余参数。
        插件可用任何方式解析（typer / click / argparse / sys.argv），
        自行处理 --help、错误提示、子命令路由、持久化、TUI、退出码。"""
```

差别：
- `validate_static` 保留——它是**安装期的框架职责**（拒绝坏插件入库），不在 dispatch 范畴。
- 其余钩子全部下沉到插件内部，由插件的 `dispatch` 自行编排。
- `StrategyPlugin` Protocol **撤销**——它本就只是 strategy 类型的 dispatch 契约，被新的统一 `Plugin.dispatch` 取代。
- `ChannelPlugin` Protocol **保留**（决策 #11）——它定义的是"channel 插件被 framework notifier 调用时"的接口（`push(payload) -> Outcome`），与 CLI dispatch 是两回事。任何 channel 插件**同时**实现两个 Protocol：`Plugin`（CLI 入口）+ `ChannelPlugin`（被 notifier 调用）。
- `PluginMetadata.type` **保留**为元信息（决策 #10）——`plugin list` 能看到分类标签，但框架的 CLI 路由行为完全不依赖 type，仅 notifier 在枚举可推送目标时按 `type=="channel"` 过滤。

### 3.3 框架不再持有的东西

| 资产 | 现位置 | 处置 |
|---|---|---|
| `cli_strategy.py` | 整个文件 | **删除** |
| `cli_channel.py` | 整个文件 | **删除** |
| `StrategyContext` | `core/context.py` | **删除**（或下沉到具体插件包；不强求复用） |
| `StrategyParams` | `plugins_api/base.py` | **删除** |
| `StrategyRunner` | `core/strategy_runner.py` | **删除**（按用户决策 #2，各插件自管运行编排） |
| `strategy_runs` 表 + 相关 core 迁移 | core 迁移文件 | **删除迁移**；现存 v0.x 库的表怎么办？因为不考虑兼容性，**直接 DROP**（参见 §5） |
| `tui/textual_dashboard.py`、`tui/*.py` | TUI 模块 | **删除**（如某个插件想要 TUI，自己挑库自己实现） |
| `core/notifier.py` + `build_notifier` | 核心 | **保留并升级为框架顶层 API**（决策 #11）。对外暴露简单接口（如 `from deeptrade import notify; notify(payload)`），框架内部根据已安装的 channel 插件路由；无 channel 时为 NoopNotifier，调用方完全无感。任何插件需要发通知都直接调，不感知具体渠道 |
| `plugins_api/base.py` 中的 `StrategyPlugin` Protocol | API 包 | **删除**，替换为 §3.2 的最小化 `Plugin` Protocol |
| `plugins_api/channel.py` 中的 `ChannelPlugin` Protocol | API 包 | **保留**（决策 #11）——它是 channel 插件被 notifier 调用时的契约（`push(payload) -> Outcome`），与 CLI dispatch 是两回事 |
| `plugins_api/notify.py`（NotificationPayload 等） | API 包 | **保留**——notifier ↔ channel 的数据契约 |
| `plugins_api/events.py` | API 包 | **保留**——可能被某些插件继续用作事件协议；如无插件依赖再删 |
| `cli_data.py` | data 命令组 | **保留**（用户决策 #4：本版本不动）。但 `cmd_sync` 内部依赖 `StrategyContext` / `StrategyParams` / `_pick_strategy_interactively`——下一版本一并处理。**本版本要做的**：把它依赖的 strategy 资产复制一份到 data 内部（或者同步把这些资产临时保留），不能让 data 跟 strategy 一起塌方 |

### 3.4 保留字与 plugin_id 校验

`PluginMetadata.plugin_id` 当前 pattern：`^[a-z][a-z0-9-]{2,31}$`，已自然排除 `--xxx`、`-X` 等。

**新增 install 期校验**（在 `plugin_manager.install` 内）：

```python
RESERVED_PLUGIN_IDS = {"init", "config", "plugin", "data", "hello"}
if metadata.plugin_id in RESERVED_PLUGIN_IDS:
    raise PluginInstallError(f"plugin_id {metadata.plugin_id!r} is reserved by the framework")
```

如本版本删掉 `hello`，则保留字集合相应缩为 `{"init", "config", "plugin", "data"}`。

### 3.5 `--help` 行为

| 输入 | 输出 |
|---|---|
| `deeptrade --help` | 框架命令清单（init / config / plugin / data）+ 一行提示"插件命令请用 `deeptrade plugin list` 查询，再用 `deeptrade <plugin_id> --help` 查看具体用法" |
| `deeptrade config --help` | 框架自管，typer/click 自动生成 |
| `deeptrade <plugin_id> --help` | 框架不拦截，原样传给 `plugin.dispatch(["--help"])`，由插件自管 |
| `deeptrade <unknown>` | 框架打印"未知命令/插件"+ 已安装插件列表 |

---

## 4. 主菜单的处置

按决策 #9 直接移除：删除 `cli.py:_interactive_main_menu` 函数及相关调用；`deeptrade` 无参数时直接打印 `--help`（移除 `invoke_without_command=True` 也行，让 typer 的默认行为接管）。

---

## 5. 数据库 schema 影响（基础）

按决策 #2 + #5：

- 删除 `strategy_runs` / `strategy_events` 表及其 DDL（在 core migrations 文件里直接摘掉）。
- 不考虑兼容，开发库 `deeptrade init` 重建即可。
- 数据库 schema 的**完整重新归位**见 §6。

---

## 6. Migration 架构修订（决策 #13）

### 6.1 现状盘点

DeepTrade 当前**已经有两套独立的 migration 机制**——这点是好的：

| 机制 | 跟踪表 | 应用时机 | 应用者 |
|---|---|---|---|
| Core migrations | `schema_migrations` | `deeptrade init` 时 | `apply_core_migrations(db)` |
| Plugin migrations | `plugin_schema_migrations`（按 plugin_id × version 分键） | `plugin install` / `plugin upgrade` 时 | `PluginManager` 根据 yaml `migrations:` 字段 |

但**框架 migrations 当前包含了一些不该属于框架的表**——这就是你指出的问题。完整盘点 core migrations（`core/migrations/core/20260427_001_init.sql` + `20260428_001_add_moneyflow.sql`）所建表：

| 表 | 当前归属 | 真实归属 | 写入方 | 处置建议 |
|---|---|---|---|---|
| `app_config` | core | **真框架** | `ConfigService` | ✅ 留 core |
| `secret_store` | core | **真框架** | `ConfigService` | ✅ 留 core |
| `schema_migrations` | core | **真框架** | core 自己 | ✅ 留 core |
| `plugins` | core | **真框架** | `PluginManager` | ✅ 留 core |
| `plugin_tables` | core | **真框架** | `PluginManager` | ✅ 留 core |
| `plugin_schema_migrations` | core | **真框架** | `PluginManager` | ✅ 留 core |
| `llm_calls` | core | **真框架**（DeepSeekClient 框架服务的审计） | `DeepSeekClient` | ✅ 留 core |
| `tushare_sync_state` | core | **真框架**（TushareClient 框架服务的幂等状态） | `TushareClient` | ✅ 留 core |
| `tushare_calls` | core | **真框架**（TushareClient 框架服务的审计） | `TushareClient` | ✅ 留 core |
| `strategy_runs` | core | strategy 类型业务 | `StrategyRunner`（要删） | ❌ **删除**（决策 #2 已涵盖） |
| `strategy_events` | core | strategy 类型业务 | `StrategyRunner`（要删） | ❌ **删除** |
| **`stock_basic`** | core (§6.2 "shared market") | **共享市场数据**——TushareClient 写、strategy 插件读 | `TushareClient.call("stock_basic")` 落表 | ⚠️ **决策点**，见 §6.3 |
| **`trade_cal`** | core | 同上 | TushareClient | ⚠️ 同上 |
| **`daily`** | core | 同上 | TushareClient | ⚠️ 同上 |
| **`daily_basic`** | core | 同上 | TushareClient | ⚠️ 同上 |
| **`moneyflow`** | core | 同上 | TushareClient | ⚠️ 同上 |

完全合规的（没有泄露到框架的）插件表：

| 表 | 来源 | 跟踪 |
|---|---|---|
| `lub_limit_list_d` / `lub_limit_ths` / `lub_stage_results` | `strategies_builtin/limit_up_board/migrations/20260427_001_init.sql` + yaml `tables:` 声明 | `plugin_schema_migrations` |
| `va_*`（volume_anomaly） | 同上模式 | 同上 |

### 6.2 已直接确定的处置

| 资产 | 处置 |
|---|---|
| `strategy_runs` / `strategy_events` 的 DDL | 直接从 core 的 init.sql 删除 |
| 现有 `lub_*` / `va_*` 插件 migrations | 不动——已经按规范走 |
| 内建插件如要保留 history / report 能力 | 各自在自己的 plugin migrations 里新增 `<prefix>_runs` 等表，并在 yaml 声明，框架完全无感 |

### 6.3 5 张共享市场数据表的处置 · 方案 A 纯隔离（决策 #14 确定）

**已决策为方案 A 纯隔离**。理由：本工具是本地单用户工具，一般不会同时跑多套策略；磁盘视为无限；与"插件自管数据"原则完全一致。

具体落地：

#### 6.3.1 框架侧改动

| 资产 | 改动 |
|---|---|
| `core/migrations/core/20260427_001_init.sql` | 删除 `stock_basic` / `trade_cal` / `daily` / `daily_basic` 4 张表的 DDL |
| `core/migrations/core/20260428_001_add_moneyflow.sql` | 整文件删除（仅含 `moneyflow` 一张） |
| `core/tushare_client.py` 的 `TushareClient` | 退化为纯 API 调用层：`call(api_name, ..., plugin_id, force_sync) → DataFrame`。**不再**把响应落到任何命名业务表 |
| `tushare_sync_state` 表 | 主键追加 `plugin_id` 维度（`PRIMARY KEY (plugin_id, api_name, trade_date)`）。每个插件独立跟踪自己的同步状态——是否 force_sync、是否已同步过 都按插件粒度判断 |
| `tushare_calls` 表 | 增加 `plugin_id` 列，仅作审计用途（无主键约束变动） |
| `llm_calls` 表 | 增加 `plugin_id` 列（已有 `plugin_id` 字段，保持不变） |

#### 6.3.2 插件侧改动

每个内建 strategy 插件在自己的 `deeptrade_plugin.yaml` + `migrations/*.sql` 里：

- 声明它实际用到的 tushare 派生表，命名带插件前缀（如 `lub_stock_basic` / `lub_trade_cal` / `lub_daily` / `lub_daily_basic` / `lub_moneyflow`，`va_*` 同理）。
- 自己负责把 `TushareClient.call(...)` 返回的 DataFrame 写入自己的表（一段 30 行内的通用工具函数即可，如有需要可放到 `plugins_api` 里作为可选 helper，但**不是**强制 API）。
- 代码内的 SQL 引用从 `stock_basic` / `daily` 等 → 改为 `lub_stock_basic` / `lub_daily` 等。

#### 6.3.3 副作用 / 注意事项

- **磁盘**：N 个安装的 strategy 插件 = N 份重复（按用户原则可接受）。
- **API 限流**：每个插件独立打 tushare，调用次数 × N。本地单用户场景下一般不会同时跑，影响有限；如果将来真有"同时跑多个策略"的诉求，再考虑加共享底座插件。
- **SQL JOIN**：失去跨插件 JOIN 能力——但本来按"插件互不耦合"原则也不该跨插件 JOIN，这是预期行为。
- **`data sync` 命令（决策 #4 本版本不动）**：当前其实现依赖 `StrategyContext` 等已删资产，本版本已经会塌方。S1 步骤需要做最小修补：要么把 `data sync` 临时禁用（命令存在但提示"本子命令重构中"），要么把它依赖的代码内联——这件事在 §7 S1 里明确。

### 6.4 Migration 工作流（重申，无论选 A/B/C 都适用）

- **框架表的迁移**：写在 `core/migrations/core/*.sql`，跟踪在 `schema_migrations`，`deeptrade init` 时增量应用；版本格式 `YYYYMMDD_NNN`。
- **插件表的迁移**：每个插件 `<plugin_pkg>/migrations/*.sql`，在 `deeptrade_plugin.yaml` 的 `migrations:` 里声明（含 sha256 校验），跟踪在 `plugin_schema_migrations(plugin_id, version)`，`plugin install` / `plugin upgrade` 时按版本顺序应用。
- **purge 规则**：`plugin uninstall --purge` 时，按 yaml `tables:` 中 `purge_on_uninstall=true` 的表 DROP；插件迁移文件**不需要**反向回滚 SQL（一刀切 DROP 即可）。
- **任何方案下，框架 migrations 和插件 migrations 互不感知、互不依赖。**

---

## 7. 实施计划（建议）

按删除先于新建的顺序，避免中间态混乱：

| 步骤 | 工作内容 | 影响面 |
|---|---|---|
| **S1** | **大删除**：`cli_strategy.py` / `cli_channel.py` / `tui/` 整包 / `core/strategy_runner.py` / `core/context.py`（StrategyContext） / `plugins_api/base.py` 中的 `StrategyParams` 与 `StrategyPlugin` Protocol；core init.sql 摘除 `strategy_runs` / `strategy_events` / `stock_basic` / `trade_cal` / `daily` / `daily_basic` 6 张表；删除 `20260428_001_add_moneyflow.sql`；`cli_data.py` 的 `cmd_sync` 临时改为打印 "data sync 子命令重构中，本版本暂不可用" + Exit 2（决策 #4 暂留命令但功能下版本恢复） | 内建插件代码暂时跑不起来 |
| **S2** | `plugins_api/base.py` 新增最小 `Plugin` Protocol（`metadata` + `validate_static` + `dispatch(argv) -> int`）；`StrategyPlugin` 的引用全部清理 | 新契约就位 |
| **S3** | 改造 `cli.py`：移除 `add_typer(strategy_app)` / `add_typer(channel_app)`；用自定义 `click.Group` 实现 §3.1 的纯透传路由；删除 `_interactive_main_menu` 和 `hello` 命令；`deeptrade` 无参数等价 `--help` | 框架命令矩阵收敛到目标 |
| **S4** | `plugin_manager.install` 增加保留字校验（拒绝 `init` / `config` / `plugin` / `data` 作为 plugin_id） | 防御性 |
| **S5** | notifier 升级为顶层 API：`deeptrade.__init__` 暴露 `notify(payload)`；内部沿用 `build_notifier`；无 channel 时 NoopNotifier | 插件零依赖发通知 |
| **S6** | **按方案 A 改造数据层**（决策 #14）：① 删上述 5 张共享市场表的 DDL（已在 S1 完成）；② `TushareClient.call` 签名加 `plugin_id`，去掉对命名业务表的写入；③ `tushare_sync_state` 主键加 `plugin_id`；④ `tushare_calls` 加 `plugin_id` 列 | 数据层架构定型 |
| **S7** | 改造内建插件 `limit_up_board` 与 `volume_anomaly`：每个插件自带 `cli.py`（typer/click 自管），实现 `run / history / report / sync` 子命令；新增 `<prefix>_stock_basic` / `<prefix>_trade_cal` / `<prefix>_daily` / `<prefix>_daily_basic` / `<prefix>_moneyflow` 等 tushare 派生表；新增 `<prefix>_runs` 等运行历史表；插件 yaml 声明 + migrations 落 DDL；插件代码 SQL 引用全部改为带前缀的本插件表 | 内建插件恢复可用 |
| **S8** | 改造 `channels_builtin/*`：channel 插件如需 CLI 自测能力，实现自己的 `dispatch`（如 `deeptrade feishu test`）；`ChannelPlugin` Protocol（被 notifier 调用的接口）保留不动 | channel 自测能力回归 |
| **S9** | 文档同步：DESIGN.md（重写 §2.8 / §6 / §9 / §12 等受影响章节）、README.md、quick-start.md、plugin-development.md | 文档 |
| **S10** | tests 全部刷一遍：删 strategy/channel CLI 测试、补框架透传路由测试、补 `Plugin` 协议契约测试、补 migration 隔离测试、补每插件自带表的 sanity 测试 | 回归 |

**分组建议**：
- **PR-1（框架破除）**：S1 + S2 + S3 + S4 + S5 + S6 — 框架收敛到目标态，但内建插件暂时跑不通。建议作为单 PR 提交，避免半成品停留在 main。
- **PR-2（内建插件重建）**：S7 + S8 — 让 `limit-up-board` / `volume-anomaly` / `feishu` 等回到可用状态。
- **PR-3（文档+测试）**：S9 + S10 — 收尾。

如希望粒度更细，PR-1 可拆成 (S1+S2+S3+S4) 与 (S5+S6) 两个，但中间态库表会比较奇怪，不强烈推荐。

---

## 8. 最终设计与计划复述

**框架最终命令矩阵**（决策 #1–#14 全部并入）：

```
deeptrade                          # = deeptrade --help
deeptrade --version | -V
deeptrade --help | -h              # 仅展示框架命令；提示通过 plugin list 查插件
deeptrade init [--no-prompts]
deeptrade config show
deeptrade config set <key> <value>
deeptrade config set-tushare       # 交互
deeptrade config set-deepseek      # 交互
deeptrade config test              # 联通性测试
deeptrade plugin install <path> [-y]
deeptrade plugin list
deeptrade plugin info <plugin_id>
deeptrade plugin enable <plugin_id>
deeptrade plugin disable <plugin_id>
deeptrade plugin uninstall <plugin_id> [--purge] [-y]
deeptrade plugin upgrade <path>
deeptrade data sync ...            # 临时禁用，下版本恢复
deeptrade <plugin_id> <argv...>    # 纯透传
```

**框架最终 schema**（仅这些）：
- `app_config` / `secret_store` / `schema_migrations`
- `plugins` / `plugin_tables` / `plugin_schema_migrations`
- `llm_calls`（带 plugin_id 列）
- `tushare_sync_state(plugin_id, api_name, trade_date)` / `tushare_calls`（带 plugin_id 列）

**框架最终 Python 公共表面**：
- `from deeptrade import notify` — 推送消息（无 channel 时 noop）
- `from deeptrade.plugins_api import Plugin, PluginMetadata, ChannelPlugin, NotificationPayload, ...` — 插件契约 + 数据契约
- `from deeptrade.core.tushare_client import TushareClient` — 数据调用
- `from deeptrade.core.deepseek_client import DeepSeekClient` — LLM 调用
- `from deeptrade.core.config import ConfigService` — 配置读写
- `from deeptrade.core.db import Database` — 数据库句柄

**插件契约**（最小集）：
```python
class Plugin(Protocol):
    metadata: PluginMetadata
    def validate_static(self, ctx) -> None: ...    # 安装期自检，无网络
    def dispatch(self, argv: list[str]) -> int: ...  # CLI 入口，自管 --help
```

`ChannelPlugin` 额外契约（仅 channel 插件实现）：
```python
class ChannelPlugin(Protocol):
    def push(self, payload: NotificationPayload) -> Outcome: ...
```

**开发计划**：S1–S10 共 10 步，建议按 PR-1（S1–S6）→ PR-2（S7–S8）→ PR-3（S9–S10）三组提交。

---

如以上无问题，回复"开始开发"即可，我将从 PR-1 起步。
