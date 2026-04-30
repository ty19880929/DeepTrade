# 策略执行界面 Textual TUI 改造方案

评估对象：`E:\personal\DeepTrade`  
改造范围：仅 `deeptrade strategy run` 的策略执行实时界面  
目标：将当前 Rich Live Dashboard 直接切换为 Textual 标准 TUI  
约束：严禁改变现有界面 UI 布局和功能；不保留 Rich dashboard 兼容分支

## 1. 改造目标

当前策略执行界面使用 `rich.Live`：

```text
Typer -> validate/configure -> StrategyRunner -> Rich Live Dashboard
```

改造后：

```text
Typer -> validate/configure -> Textual StrategyRunApp -> 后台执行 StrategyRunner
```

本次改造只替换实时执行界面，不改变策略执行语义。

保持不变：

- CLI 命令与参数。
- 插件接口。
- `StrategyRunner` 执行流程。
- `StrategyEvent` 事件模型。
- DuckDB 持久化逻辑。
- 报告生成逻辑。
- `plugin.render_result()` 执行后摘要展示。
- `--no-dashboard` 文本模式。

## 2. 当前结构分析

当前策略执行入口位于：

```text
deeptrade/cli_strategy.py
```

核心逻辑：

```python
with Dashboard(state) as dash:
    outcome = runner.execute(
        plugin,
        ctx_pre,
        params,
        plugin_id=plugin_id,
        on_event=dash.consume,
        run_id=run_id,
    )
    dash.mark_finished(outcome.status)
```

当前 Rich dashboard 位于：

```text
deeptrade/tui/dashboard.py
deeptrade/tui/widgets.py
```

可复用资产：

- `DashboardState`
- `StepState`
- `DashboardState.apply_event()`
- `widgets.py` 中的 Rich render helper
- `StrategyEvent`
- `StrategyRunner.execute(..., on_event=...)`

这些结构已经将“策略执行”和“界面消费事件”解耦，因此适合局部替换 UI 层。

## 3. UI 布局要求

Textual 版本必须保持当前 UI 信息结构和布局，不新增、不删除核心区域。

目标布局：

```text
┌ Header ─────────────────────────────────────────────┐
│ NERV ▌ DEEPTRADE   plugin_id   run_id   started     │
└─────────────────────────────────────────────────────┘

[可选 Banner 区：INTRADAY / PARTIAL / FAILED]

┌ Progress ───────────────┐ ┌ Events ─────────────────┐
│ Step / Status           │ │ Time / Type / Message   │
│ ...                     │ │ ...                     │
└─────────────────────────┘ └─────────────────────────┘

┌ Live ───────────────────────────────────────────────┐
│ [latest event type] latest event message            │
└─────────────────────────────────────────────────────┘

┌ Footer ─────────────────────────────────────────────┐
│ LLM n │ Tokens x↑ y↓ │ TS n │ elapsed               │
└─────────────────────────────────────────────────────┘
```

区域说明：

| 区域 | 功能 | 是否保持 |
|---|---|---|
| Header | 展示产品名、插件、run_id、开始时间 | 必须保持 |
| Banner | 展示 intraday / partial / failed 状态 | 必须保持 |
| Progress | 展示策略步骤和状态 | 必须保持 |
| Events | 展示事件流 | 必须保持 |
| Live | 展示最新事件 / 当前执行状态 | 必须保持 |
| Footer | 展示 LLM、token、Tushare、耗时统计 | 必须保持 |

## 4. 总体设计

新增文件：

```text
deeptrade/tui/textual_dashboard.py
```

建议包含：

```python
class StrategyEventMessage(Message): ...
class RunFinishedMessage(Message): ...
class StrategyRunApp(App): ...
def run_strategy_tui(...) -> RunOutcome: ...
```

`run_strategy_tui()` 作为 `cli_strategy.py` 的调用入口，屏蔽 Textual app 生命周期。

## 5. Textual App 设计

### 5.1 App 输入参数

```python
class StrategyRunApp(App):
    def __init__(
        self,
        *,
        plugin,
        ctx,
        params,
        plugin_id: str,
        run_id: str,
        db: Database,
    ) -> None:
        ...
```

需要持有：

- `plugin`
- `StrategyContext`
- `StrategyParams`
- `plugin_id`
- `run_id`
- `Database`
- `DashboardState`
- `RunOutcome | None`

### 5.2 Widget 结构

推荐第一版使用 `Static` 承载 Rich renderable，以最大程度保持现有视觉一致。

```python
class StrategyRunApp(App):
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Static(id="banner")
        with Horizontal(id="main"):
            yield Static(id="progress")
            yield Static(id="events")
        yield Static(id="live")
        yield Static(id="footer")
```

CSS 只负责布局尺寸，不重新设计视觉风格。

### 5.3 复用 Rich render helper

为了确保 UI 不变，首版不要立即改为 Textual `DataTable`。

继续复用：

- `progress_table(self.state.steps)`
- `events_table(...)`
- `live_status_panel(...)`
- `footer_text(...)`
- `banners_text(...)`

各 `Static` 更新 Rich renderable：

```python
self.query_one("#progress", Static).update(
    Panel(progress_table(self.state.steps), ...)
)
```

这样 Textual 负责局部刷新和事件循环，Rich 负责现有样式渲染。

## 6. 执行模型

### 6.1 后台执行策略

Textual App 启动后，在后台 worker 中执行策略。

```python
def on_mount(self) -> None:
    self._render_all()
    self.run_worker(self._execute_strategy, thread=True)
```

后台线程执行：

```python
def _execute_strategy(self) -> None:
    runner = StrategyRunner(self.db)
    outcome = runner.execute(
        self.plugin,
        self.ctx,
        self.params,
        plugin_id=self.plugin_id,
        on_event=self._on_strategy_event,
        run_id=self.run_id,
    )
    self.call_from_thread(
        self.post_message,
        RunFinishedMessage(outcome),
    )
```

### 6.2 事件投递

`on_event` 由 runner 在线程中调用，不能直接更新 Textual widget。

```python
def _on_strategy_event(self, ev: StrategyEvent) -> None:
    self.call_from_thread(
        self.post_message,
        StrategyEventMessage(ev),
    )
```

Textual 主线程中处理：

```python
def on_strategy_event_message(self, msg: StrategyEventMessage) -> None:
    self.state.apply_event(msg.event)
    self._render_changed_by_event(msg.event)
```

### 6.3 运行结束

```python
def on_run_finished_message(self, msg: RunFinishedMessage) -> None:
    self.outcome = msg.outcome
    self.state.mark_finished(msg.outcome.status)
    self._render_all()
    self.exit(msg.outcome)
```

默认执行完成后自动退出 Textual，回到 `cli_strategy.py` 继续执行原有逻辑：

```python
plugin.render_result(ctx_pre, outcome.run_id)
typer.echo(f"\nstatus: {outcome.status.value}  run_id: {outcome.run_id}")
```

这样不会改变用户执行完成后的输出行为。

## 7. CLI 改造

### 7.1 修改默认 dashboard 分支

当前：

```python
if use_dashboard:
    state = DashboardState(...)
    with Dashboard(state) as dash:
        outcome = runner.execute(..., on_event=dash.consume)
        dash.mark_finished(outcome.status)
```

改为：

```python
if use_dashboard:
    from deeptrade.tui.textual_dashboard import run_strategy_tui

    outcome = run_strategy_tui(
        plugin=plugin,
        ctx=ctx_pre,
        params=params,
        plugin_id=plugin_id,
        run_id=run_id,
        db=db,
    )
```

`--no-dashboard` 分支保持不变：

```python
else:
    outcome = runner.execute(...)
    for ev in outcome.seen_events:
        typer.echo(...)
```

### 7.2 删除 Rich Dashboard 兼容

本次要求“不需要兼容，直接切到 Textual”。

完成 Textual 替换后：

- `deeptrade strategy run` 默认使用 Textual。
- 不提供 `--ui rich`。
- 不保留 Rich dashboard 运行路径。
- `dashboard.py` 可在测试稳定后删除或保留为未引用代码。

建议第一步先取消引用，第二步再删除文件，降低一次性改动风险。

## 8. 依赖变更

`pyproject.toml` 增加：

```toml
dependencies = [
    ...
    "textual>=0.80",
]
```

如果希望使用较新的 Worker API，可改为：

```toml
"textual>=1.0"
```

需要同步更新 lock 文件：

```bash
uv lock
```

## 9. 刷新策略

Textual 下不要整屏重画。按事件类型刷新局部区域：

| 事件 | 刷新区域 |
|---|---|
| `STEP_STARTED` | Progress, Events, Live |
| `STEP_PROGRESS` | Progress, Events, Live |
| `STEP_FINISHED` | Progress, Events, Live |
| `LLM_BATCH_STARTED` | Events, Live |
| `LLM_BATCH_FINISHED` | Footer, Events, Live |
| `LLM_FINAL_RANK` | Footer, Events, Live |
| `TUSHARE_CALL` | Footer, Events, Live |
| `TUSHARE_FALLBACK` | Footer, Events, Live |
| `VALIDATION_FAILED` | Progress, Events, Live |
| `RESULT_PERSISTED` | Events, Live |
| run finished | Banner, Progress, Live, Footer |

Footer 耗时如果需要持续更新，可使用：

```python
self.set_interval(1.0, self._render_footer)
```

只刷新 footer，不刷新整屏。

## 10. 键盘行为

第一版保持最小行为，避免改变策略语义。

建议：

| 按键 | 行为 |
|---|---|
| `q` | 若策略已结束，退出界面；若运行中，提示正在执行 |
| `Ctrl+C` | 交给 Textual / 外层中断处理 |

注意：当前 runner 没有 cancellation token。长时间 Tushare / LLM 调用期间无法优雅中断。

因此第一版不实现复杂取消，只保证：

- 正常执行完成自动退出。
- 异常执行完成自动退出并返回 `RunOutcome`。
- `--no-dashboard` 仍保留原有中断行为。

后续如需真正取消，应另行设计：

- `CancellationToken`
- runner 检查取消状态
- Tushare / LLM 调用超时和中断策略

## 11. 线程与数据库约束

最大技术风险是 Textual 主线程与后台执行线程的边界。

原则：

1. 后台 worker 独占执行 `StrategyRunner.execute()`。
2. 后台 worker 执行所有 DB 写入。
3. Textual UI 主线程只消费 `StrategyEvent`，不直接读写 DB。
4. UI 线程不要调用 `ctx.db.execute()`。
5. `plugin.render_result()` 仍在 Textual 退出后由 CLI 主流程调用。

当前 `Database` 有写锁，但仍不建议让 UI 线程和 worker 同时使用同一个 DuckDB connection。

## 12. 测试方案

保留现有状态模型测试：

- `DashboardState.apply_event()`
- `StepState`
- events buffer
- token/tushare counters

新增 Textual 测试：

1. App 能 mount 成功。
2. 收到 `STEP_STARTED` 后 Progress 区更新。
3. 收到 `STEP_FINISHED` 后 step 状态变为 completed。
4. 收到 `LLM_BATCH_FINISHED` 后 footer token 统计更新。
5. 收到 `VALIDATION_FAILED` 后当前 step 标记 error。
6. 收到 `RunFinishedMessage(success)` 后 final status 更新并退出。
7. `strategy run` 默认使用 Textual 分支。
8. `strategy run --no-dashboard` 仍走文本模式。
9. Textual 执行返回的 `RunOutcome` 与 runner 返回一致。

测试工具：

```python
async with app.run_test() as pilot:
    ...
```

## 13. 风险评估

| 风险 | 等级 | 说明 | 缓解 |
|---|---|---|---|
| 后台线程与 DB 连接边界 | 中 | Textual 需要 worker 执行策略 | UI 线程不读写 DB |
| UI 视觉偏差 | 中 | Textual 原生组件可能改变外观 | 首版用 `Static + Rich renderable` |
| Ctrl+C / 取消行为 | 中 | 当前 runner 无 cancellation token | 第一版不实现复杂取消 |
| 测试复杂度增加 | 中 | Textual 测试是 async/pilot 模式 | 保留状态模型单测，只少量集成测试 |
| Windows 终端兼容 | 低到中 | Textual 通常优于 Rich Live，但仍需实测 | PowerShell / Windows Terminal 手工验证 |
| 依赖复杂度上升 | 中 | 新增 Textual 依赖 | 仅用于 strategy run TUI |

## 14. 实施步骤

### Step 1：引入依赖

修改 `pyproject.toml`：

```toml
"textual>=0.80"
```

执行：

```bash
uv lock
uv sync
```

### Step 2：新增 Textual dashboard

新增：

```text
deeptrade/tui/textual_dashboard.py
```

实现：

- `StrategyEventMessage`
- `RunFinishedMessage`
- `StrategyRunApp`
- `run_strategy_tui()`

### Step 3：复用当前状态模型和 render helper

从现有代码复用：

```python
from deeptrade.tui.dashboard import DashboardState
from deeptrade.tui.widgets import (
    banners_text,
    events_table,
    footer_text,
    live_status_panel,
    progress_table,
)
```

如果后续删除 `dashboard.py`，则将 `DashboardState` 移到：

```text
deeptrade/tui/dashboard_state.py
```

### Step 4：切换 `cli_strategy.py`

将 Rich `Dashboard` 调用替换为 `run_strategy_tui()`。

删除：

```python
from deeptrade.tui.dashboard import Dashboard, DashboardState
```

新增：

```python
from deeptrade.tui.textual_dashboard import run_strategy_tui
```

### Step 5：保留 `--no-dashboard`

确保非 TTY / CI / 用户显式禁用时仍走文本模式。

### Step 6：补测试

新增：

```text
tests/tui/test_textual_dashboard.py
```

修改：

```text
tests/cli/test_strategy_run_cmd.py
```

覆盖默认 Textual 分支和 `--no-dashboard` 分支。

### Step 7：清理旧 Rich dashboard

如果所有测试通过：

- 删除未使用的 `Dashboard` 类。
- 如仍需 `DashboardState`，迁移到独立文件。
- 保留 `widgets.py`，因为 Textual 首版仍复用 Rich renderable。

## 15. 验收标准

功能验收：

- `deeptrade strategy run` 默认进入 Textual TUI。
- `deeptrade strategy run --no-dashboard` 仍输出文本事件。
- 策略执行成功后仍展示 `render_result()` 摘要。
- 失败 / partial_failed / cancelled 状态仍正确显示。
- `strategy_runs`、`strategy_events`、报告文件输出不变。

UI 验收：

- Header / Banner / Progress / Events / Live / Footer 六个区域仍存在。
- 字段、文案、统计口径与原界面一致。
- 内容更新时不再出现 Rich Live 整屏闪烁。
- 终端窗口 resize 后布局仍可用。

测试验收：

```bash
uv run pytest
uv run ruff check .
uv run mypy deeptrade
```

全部通过。

## 16. 结论

本项目当前结构适合进行局部 Textual 迁移。`StrategyRunner` 和 `StrategyEvent` 已经提供了清晰的事件边界，改造重点集中在 UI 事件循环和后台执行线程。

推荐采用“Textual 管理布局与局部刷新，Rich renderable 保持视觉一致”的方案。这样可以满足“直接切到 Textual 标准 TUI”的目标，同时最大限度避免 UI 布局和功能变化。
