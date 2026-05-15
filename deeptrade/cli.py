"""DeepTrade CLI entry point.

Framework command surface (closed):

    deeptrade --version | -V
    deeptrade --help    | -h
    deeptrade init [--no-prompts]
    deeptrade config {show, set, set-tushare, set-llm, list-llm, test-llm}
    deeptrade plugin {install, list, info, enable, disable, uninstall, upgrade}
    deeptrade data sync ...                 (stub — pending refactor)

Plugin commands are dispatched through pure pass-through:

    deeptrade <plugin_id> <argv...>         → plugin.dispatch(argv)

The framework knows nothing about a plugin's subcommand tree. ``--help`` for a
plugin is the plugin's own responsibility.
"""

from __future__ import annotations

import sys

import click
import typer
from typer.core import TyperGroup

from deeptrade import __version__
from deeptrade.cli_config import app as config_app
from deeptrade.cli_data import app as data_app
from deeptrade.cli_plugin import app as plugin_app
from deeptrade.core import paths
from deeptrade.core.db import Database, apply_core_migrations

# ---------------------------------------------------------------------------
# Custom click.Group implementing pure plugin pass-through
# ---------------------------------------------------------------------------


class _DeepTradeGroup(TyperGroup):
    """Top-level group that falls back to plugin dispatch for unknown commands.

    Resolution order on ``deeptrade <token> ...``:

        1. If ``<token>`` is a registered framework command → click handles it.
        2. Otherwise look it up in the ``plugins`` table:
             - found + enabled  → load entrypoint, call ``plugin.dispatch(argv)``
             - found + disabled → exit 2 with "enable first" hint
             - not found        → exit 2 with "unknown" + framework cmd list
    """

    # Disable click's "no such command" so we can route to plugins instead.
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        registered = super().get_command(ctx, cmd_name)
        if registered is not None:
            return registered
        return _build_plugin_command(cmd_name)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Standard resolution: returns (cmd_name, cmd, remaining_args).
        cmd_name = args[0] if args else None
        cmd = self.get_command(ctx, cmd_name) if cmd_name else None
        if cmd is None and cmd_name is not None:
            # Synthesize a helpful error including framework cmd list.
            framework = sorted(super().list_commands(ctx))
            ctx.fail(
                f"未知命令或插件: {cmd_name!r}\n"
                f"  框架命令: {framework}\n"
                f"  执行 `deeptrade plugin list` 查看已安装插件"
            )
        return super().resolve_command(ctx, args)


def _build_plugin_command(plugin_id: str) -> click.Command | None:
    """Resolve ``plugin_id`` against the installed plugins; return a click
    Command that dispatches to ``plugin.dispatch(remaining_argv)``."""
    from pathlib import Path

    from deeptrade.core.plugin_manager import (
        PluginManager,
        PluginNotFoundError,
        _load_entrypoint,
    )

    db = Database(paths.db_path())
    try:
        mgr = PluginManager(db)
        try:
            rec = mgr.info(plugin_id)
        except PluginNotFoundError:
            return None
    finally:
        db.close()

    if not rec.enabled:

        @click.command(
            name=plugin_id,
            help=f"(disabled / 已禁用) {rec.name}",
            context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
        )
        def _disabled() -> None:
            typer.echo(
                f"✘ 插件 {plugin_id!r} 已 disabled；请先执行 `deeptrade plugin enable {plugin_id}`"
            )
            raise typer.Exit(2)

        return _disabled

    # Enabled: hand the remaining argv straight to the plugin.
    @click.command(
        name=plugin_id,
        # Plugin owns its own --help; let everything through unparsed.
        help=f"{rec.name} (v{rec.version}) — 插件自管 CLI，可执行 `--help` 查看子命令",
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
            "help_option_names": [],  # don't intercept --help
        },
    )
    @click.pass_context
    def _dispatch(ctx: click.Context) -> None:
        plugin = _load_entrypoint(Path(rec.install_path), rec.entrypoint, rec.metadata)
        if not hasattr(plugin, "dispatch"):
            typer.echo(f"✘ 插件 {plugin_id!r} 未实现 dispatch()")
            raise typer.Exit(2)
        try:
            rc = plugin.dispatch(list(ctx.args))
        except (SystemExit, KeyboardInterrupt):
            # Exit codes / Ctrl-C must propagate unaltered.
            raise
        except BaseException as e:  # noqa: BLE001 — final safety net
            # Plugins are encouraged to install their own dispatch-tail handler
            # (see plugins_api.render_exception). This catch only fires when a
            # plugin lets an exception escape — we still want DEEPTRADE_DEBUG=1
            # to surface the traceback rather than a bare crash.
            from deeptrade.plugins_api import render_exception

            sys.stderr.write(render_exception(e) + "\n")
            raise typer.Exit(1) from e
        raise typer.Exit(rc or 0)

    return _dispatch


# ---------------------------------------------------------------------------
# Typer application (framework commands only)
# ---------------------------------------------------------------------------


app = typer.Typer(
    name="deeptrade",
    help="DeepTrade — LLM 驱动的 A 股选股 CLI；管理配置 / 插件 / 数据库。",
    no_args_is_help=True,
    add_completion=True,
    cls=_DeepTradeGroup,
)
app.add_typer(config_app, name="config")
app.add_typer(plugin_app, name="plugin")
app.add_typer(data_app, name="data")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"DeepTrade {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: ARG001 — consumed by Typer via callback
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="显示版本号并退出。",
    ),
) -> None:
    """DeepTrade — LLM 驱动的 A 股选股 CLI；管理配置 / 插件 / 数据库。"""


@app.command()
def init(
    no_prompts: bool = typer.Option(
        False,
        "--no-prompts",
        help="跳过初始化后的 Tushare / LLM 配置交互提示。",
    ),
) -> None:
    """初始化 ~/.deeptrade 目录并应用核心 schema 迁移（幂等）。"""
    paths.ensure_layout()
    db_file = paths.db_path()
    fresh = not db_file.exists()
    # auto_migrate=False so we can collect & print the precise list of versions
    # newly applied below; the auto-migrate path swallows that information by
    # design (it runs unconditionally on every Database open).
    db = Database(db_file, auto_migrate=False)
    try:
        applied = apply_core_migrations(db)
        if fresh:
            typer.echo(f"✔ 已创建数据库：{db_file}")
        for v in applied:
            typer.echo(f"✔ 已应用 schema 迁移：{v}")
    finally:
        db.close()

    if no_prompts or not sys.stdin.isatty():
        return

    import questionary

    if questionary.confirm("现在配置 Tushare 吗？", default=True).ask():
        from deeptrade.cli_config import cmd_set_tushare

        cmd_set_tushare()
    if questionary.confirm("现在配置一个 LLM provider 吗？", default=True).ask():
        from deeptrade.cli_config import cmd_set_llm

        cmd_set_llm()


@app.command(name="db", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def db_cmd(ctx: click.Context) -> None:
    """数据库迁移与管理命令（占位；实际入口在 `db_app` typer group）。"""
    pass


db_app = typer.Typer(name="db", help="数据库迁移与管理命令。")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init() -> None:
    """初始化核心数据库表并应用迁移。"""
    paths.ensure_layout()
    db_file = paths.db_path()
    fresh = not db_file.exists()
    # auto_migrate=False so this command reports which versions it applied;
    # the Database auto-migrate path returns nothing.
    db = Database(db_file, auto_migrate=False)
    try:
        applied = apply_core_migrations(db)
        if fresh:
            typer.echo(f"✔ 已创建数据库：{db_file}")
        if applied:
            for v in applied:
                typer.echo(f"✔ 已应用 schema 迁移：{v}")
        else:
            typer.echo("✔ 数据库已初始化；schema 已是最新")
    finally:
        db.close()


@db_app.command("upgrade")
def db_upgrade() -> None:
    """应用所有未应用的核心迁移。"""
    db_init()


if __name__ == "__main__":
    app()
