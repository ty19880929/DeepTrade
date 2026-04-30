# 写一个新插件

DeepTrade 把插件当作**一等公民**：插件 = 一个独立的目录，包含 YAML 元数据 + SQL migrations + Python 入口类。框架做的事**很少**——加载、验证、按 `plugin_id` 透传 CLI 参数。其余（怎么解析参数、怎么执行、怎么持久化、怎么渲染）**全部**归插件。

本文以一个最小可用的"价格突破"策略示意。

## 0. 插件契约（Plugin Protocol）

```python
class Plugin(Protocol):
    metadata: PluginMetadata

    def validate_static(self, ctx: PluginContext) -> None:
        """安装期自检（NO network）。"""

    def dispatch(self, argv: list[str]) -> int:
        """CLI 入口。argv 是去掉 plugin_id 后的剩余参数。返回退出码。"""
```

通知渠道额外实现 `ChannelPlugin`：

```python
class ChannelPlugin(Plugin, Protocol):
    def push(self, ctx: PluginContext, payload: NotificationPayload) -> None: ...
```

`PluginContext` 是框架在**安装期**和**通道 push 时**提供的最小服务束（`db` + `config` + `plugin_id`）。其它一切（TushareClient / DeepSeekClient / 自己的运行历史表 / TUI / Notifier）都在插件 `dispatch` 内部按需构造。

## 1. 目录结构

```
my_breakout/
├── deeptrade_plugin.yaml           # 元数据（必需）
├── README.md                       # 推荐
└── my_breakout/                    # 与 entrypoint 模块名一致
    ├── __init__.py
    ├── plugin.py                   # Plugin Protocol 入口类
    ├── cli.py                      # 你的 CLI 子命令（typer / click / argparse 任意）
    ├── runtime.py                  # 你自己的服务束（db / tushare / llm / run_id ...）
    ├── runner.py                   # 业务编排（可选拆分）
    └── migrations/
        └── 20260501_001_init.sql
```

`limit_up_board` 与 `volume_anomaly` 两个内建 strategy 插件就是这个模式的参考实现，可直接 copy 改写。

## 2. 元数据（YAML）

```yaml
plugin_id: my-breakout                    # kebab-case，全局唯一；不可与框架命令冲突
name: 我的突破策略
version: 0.1.0
type: strategy                            # 'strategy' | 'channel' | 你将来定义的
api_version: "1"
entrypoint: my_breakout.plugin:MyBreakoutPlugin
description: 简单的 60 日新高突破筛选

permissions:
  tushare_apis:
    required:
      - stock_basic
      - trade_cal
      - daily
    optional:
      - daily_basic
  llm: false
  llm_tools: false                        # 硬约束：必须 false

migrations:
  - version: "20260501_001"
    file: migrations/20260501_001_init.sql
    checksum: "sha256:<sha256 of the SQL file>"

tables:
  # 声明你的插件拥有的所有表（含从 tushare 派生的业务表）
  - name: mybk_daily                      # 你自己的 daily 副本
    description: tushare daily 落库（本插件持有）
    purge_on_uninstall: true
  - name: mybk_signals
    description: 每日突破信号
    purge_on_uninstall: true
  - name: mybk_runs
    description: 本插件 run 历史
    purge_on_uninstall: true
```

**保留字**：`plugin_id` 不能等于 `init` / `config` / `plugin` / `data`（框架命令名）。

**checksum** 必须严格匹配迁移文件的 sha256：

```bash
python -c "import hashlib; print('sha256:'+hashlib.sha256(open('migrations/20260501_001_init.sql','rb').read()).hexdigest())"
```

## 3. 数据隔离原则（重要）

> **每个插件自己拥有自己的表**——包括从 tushare 派生的业务数据。

如果你需要 `daily / stock_basic / moneyflow` 等数据，**不要**假设有"全局共享表"——它们不存在。在你自己的 migrations 里声明 `mybk_daily / mybk_stock_basic / ...`，然后调 `TushareClient.call("daily", ...)` 把返回的 DataFrame 写到自己的表里。

框架仅持有：`app_config / secret_store / schema_migrations / plugins / plugin_tables / plugin_schema_migrations / llm_calls / tushare_sync_state / tushare_calls`。所有这些都按 `plugin_id` 隔离。

## 4. 迁移 SQL

`migrations/20260501_001_init.sql`：

```sql
CREATE TABLE IF NOT EXISTS mybk_daily (
    ts_code VARCHAR, trade_date VARCHAR,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

CREATE TABLE IF NOT EXISTS mybk_signals (
    run_id     UUID NOT NULL,
    trade_date VARCHAR NOT NULL,
    ts_code    VARCHAR NOT NULL,
    signal     VARCHAR NOT NULL,
    score      DOUBLE,
    PRIMARY KEY (run_id, ts_code)
);

CREATE TABLE IF NOT EXISTS mybk_runs (
    run_id     UUID PRIMARY KEY,
    trade_date VARCHAR NOT NULL,
    status     VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP
);
```

升级时**追加**新版本文件（如 `20260601_001_add_index.sql`），框架根据 `plugin_schema_migrations` 自动跳过已应用版本。

## 5. Plugin 入口类

`my_breakout/plugin.py`：

```python
from typing import TYPE_CHECKING

from . import cli as _cli

if TYPE_CHECKING:
    from deeptrade.plugins_api import PluginContext


class MyBreakoutPlugin:
    metadata = None  # framework injects after install

    def validate_static(self, ctx: PluginContext) -> None:
        # NO network. Sanity-import only — verify your modules load.
        from . import runner  # noqa: F401

    def dispatch(self, argv: list[str]) -> int:
        return _cli.main(argv)
```

## 6. CLI 子命令树

`my_breakout/cli.py`（用 typer 写最快；click / argparse 也可以）：

```python
import sys
from typing import Optional
import typer

app = typer.Typer(name="my-breakout", help="60 日新高突破策略", no_args_is_help=True)


@app.command("run")
def cmd_run(
    trade_date: Optional[str] = typer.Option(None, "--trade-date"),
    force_sync: bool = typer.Option(False, "--force-sync"),
) -> None:
    from .runner import execute
    execute(trade_date=trade_date, force_sync=force_sync)


@app.command("history")
def cmd_history(limit: int = typer.Option(20, "--limit")) -> None:
    from deeptrade.core import paths
    from deeptrade.core.db import Database
    db = Database(paths.db_path())
    try:
        rows = db.fetchall(
            "SELECT run_id, trade_date, status FROM mybk_runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,))
    finally:
        db.close()
    for r in rows:
        typer.echo(f"{r[0]}  {r[1]}  {r[2]}")


def main(argv: list[str]) -> int:
    """Entry called by Plugin.dispatch — must return an int exit code."""
    try:
        app(argv, standalone_mode=False)
        return 0
    except typer.Exit as e:
        return int(e.exit_code or 0)
    except SystemExit as e:
        try:
            return int(e.code or 0)
        except (TypeError, ValueError):
            return 1
    except Exception as e:
        sys.stderr.write(f"✘ {type(e).__name__}: {e}\n")
        return 1
```

## 7. 自管 Runtime + 业务

`my_breakout/runtime.py` — 自己组装 db / config / tushare / llm 客户端，参考 `limit_up_board/runtime.py`：

```python
from dataclasses import dataclass
from deeptrade.core import paths
from deeptrade.core.config import ConfigService
from deeptrade.core.db import Database
from deeptrade.core.tushare_client import TushareClient, TushareSDKTransport

PLUGIN_ID = "my-breakout"


@dataclass
class MybkRuntime:
    db: Database
    config: ConfigService
    plugin_id: str = PLUGIN_ID
    run_id: str | None = None


def open_runtime() -> MybkRuntime:
    db = Database(paths.db_path())
    return MybkRuntime(db=db, config=ConfigService(db))


def build_tushare(rt: MybkRuntime) -> TushareClient:
    token = rt.config.get("tushare.token")
    if not token:
        raise RuntimeError("tushare.token not configured")
    cfg = rt.config.get_app_config()
    return TushareClient(
        rt.db, TushareSDKTransport(str(token)),
        plugin_id=rt.plugin_id, rps=cfg.tushare_rps,
    )
```

调用 tushare 时框架会按你的 `plugin_id` 隔离 `tushare_sync_state` / `tushare_calls` / `tushare_cache_blob`。

## 8. 推送通知（可选）

任何插件想发消息：

```python
from deeptrade import notify
from deeptrade.plugins_api import NotificationPayload, NotificationSection
from deeptrade.core.run_status import RunStatus

payload = NotificationPayload(
    plugin_id="my-breakout", run_id=str(run_id), status=RunStatus.SUCCESS,
    title="60 日突破信号", summary=f"{n} 只命中",
    sections=[NotificationSection(key="hits", title="命中标的", items=[...])],
)
notify(rt.db, payload)   # 自动派发到所有 enabled channel；无 channel 时 noop
```

## 9. 安装、运行、卸载

```bash
# 安装
deeptrade plugin install /path/to/my_breakout -y

# 看元数据
deeptrade plugin info my-breakout

# 运行（CLI 完全归你）
deeptrade my-breakout --help
deeptrade my-breakout run --force-sync
deeptrade my-breakout history

# 升级
# 在 my_breakout/ 中 bump version → 添加新 migration（保留旧的）→
deeptrade plugin upgrade /path/to/my_breakout

# 卸载
deeptrade plugin uninstall my-breakout            # 默认仅 disable
deeptrade plugin uninstall my-breakout --purge    # DROP 所有 tables
```

## 10. 测试建议

- 自己的业务逻辑：常规 pytest，桩 TushareClient 用 `FixtureTransport`（`deeptrade.core.tushare_client.FixtureTransport`）。
- 插件契约：用 `isinstance(MyPlugin(), Plugin)` 做 runtime 检查（Plugin Protocol 是 `runtime_checkable`）。
- 框架透传路由：参见 `tests/cli/test_routing.py` 中的 `_install_fake_plugin` 模式。

## 11. 写一个 channel 插件

`type: channel` + 实现 `Plugin` 的全部 + 加一个 `push(ctx, payload)`。参考 `deeptrade/channels_builtin/stdout/stdout_channel/channel.py`。channel 插件被 `deeptrade.notify(...)` 自动发现并路由。

## 12. 写其它类型的插件

`PluginMetadata.type` 是元信息字段，框架不依赖。你可以用任意字符串（如 `"skin"` / `"datasource"` / `"backtest"`）来描述类型，仅供 `plugin info` 展示与你自己代码内的过滤使用。
