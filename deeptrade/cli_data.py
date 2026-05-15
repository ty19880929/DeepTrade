"""`deeptrade data` subcommand group.

Currently a placeholder: the previous implementation depended on framework
assets (StrategyContext / StrategyParams / StrategyRunner) that were removed
in the v0.2 framework reshape. The ``data sync`` capability will be restored
in a later iteration as part of the per-plugin data ownership model; see
``CHANGELOG.md`` v0.2.0 for the rationale behind the deletion.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="同步某个插件的数据（不执行其主流程）。",
    no_args_is_help=True,
)


@app.command("sync")
def cmd_sync() -> None:
    """占位命令——数据层重构期间暂停使用。

    Run the plugin's own sync command directly, e.g.
    ``deeptrade <plugin_id> sync ...``.
    """
    typer.echo(
        "✘ `deeptrade data sync` 在数据层重构期间已暂停使用 "
        "(temporarily disabled).\n"
        "  请改用插件自身的 sync 子命令，例如 "
        "`deeptrade <plugin_id> sync ...`。"
    )
    raise typer.Exit(2)
