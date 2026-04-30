"""`deeptrade plugin` subcommand group."""

from __future__ import annotations

from pathlib import Path

import questionary
import typer
import yaml
from rich.console import Console
from rich.table import Table

from deeptrade.core import paths
from deeptrade.core.db import Database
from deeptrade.core.plugin_manager import (
    PluginInstallError,
    PluginManager,
    PluginNotFoundError,
    _load_metadata_yaml,
    summarize_for_install,
)

app = typer.Typer(help="Install / manage plugins", no_args_is_help=True)


def _open() -> tuple[Database, PluginManager]:
    db = Database(paths.db_path())
    return db, PluginManager(db)


@app.command("install")
def cmd_install(
    path: Path = typer.Argument(..., help="Local path to the plugin source directory"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Install a plugin from a local directory (no network)."""
    if not path.is_dir():
        typer.echo(f"Not a directory: {path}")
        raise typer.Exit(2)

    # Load + validate metadata before any DB access
    try:
        meta = _load_metadata_yaml(path / "deeptrade_plugin.yaml")
    except PluginInstallError as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    typer.echo("─── 即将安装 ─────────────────────────────")
    typer.echo(summarize_for_install(meta, path))
    typer.echo("──────────────────────────────────────────")
    if not yes:
        ok = questionary.confirm("确认安装?", default=False).ask()
        if not ok:
            typer.echo("Aborted.")
            raise typer.Exit(1)

    db, mgr = _open()
    try:
        rec = mgr.install(path)
    except PluginInstallError as e:
        typer.echo(f"✘ Install failed: {e}")
        raise typer.Exit(2) from e
    finally:
        db.close()

    typer.echo(f"✔ 已安装: {rec.plugin_id} v{rec.version}")


@app.command("list")
def cmd_list() -> None:
    """List installed plugins."""
    db, mgr = _open()
    try:
        records = mgr.list_all()
    finally:
        db.close()

    console = Console()
    table = Table(title="Installed Plugins")
    table.add_column("plugin_id", style="cyan")
    table.add_column("name")
    table.add_column("version")
    table.add_column("enabled", style="green")
    if not records:
        typer.echo("(no plugins installed)")
        return
    for r in records:
        table.add_row(r.plugin_id, r.name, r.version, "yes" if r.enabled else "no")
    console.print(table)


@app.command("info")
def cmd_info(plugin_id: str = typer.Argument(...)) -> None:
    """Show metadata + installed tables for a plugin."""
    db, mgr = _open()
    try:
        try:
            rec = mgr.info(plugin_id)
        except PluginNotFoundError as e:
            typer.echo(f"✘ {plugin_id} not installed")
            raise typer.Exit(2) from e
        typer.echo(yaml.safe_dump(rec.metadata.model_dump(mode="json"), allow_unicode=True))
    finally:
        db.close()


@app.command("disable")
def cmd_disable(plugin_id: str = typer.Argument(...)) -> None:
    db, mgr = _open()
    try:
        try:
            mgr.disable(plugin_id)
        except PluginNotFoundError as e:
            typer.echo(f"✘ {plugin_id} not installed")
            raise typer.Exit(2) from e
        typer.echo(f"✔ disabled: {plugin_id}")
    finally:
        db.close()


@app.command("enable")
def cmd_enable(plugin_id: str = typer.Argument(...)) -> None:
    db, mgr = _open()
    try:
        try:
            mgr.enable(plugin_id)
        except PluginNotFoundError as e:
            typer.echo(f"✘ {plugin_id} not installed")
            raise typer.Exit(2) from e
        typer.echo(f"✔ enabled: {plugin_id}")
    finally:
        db.close()


@app.command("uninstall")
def cmd_uninstall(
    plugin_id: str = typer.Argument(...),
    purge: bool = typer.Option(False, "--purge", help="DROP plugin tables and forget all data"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
) -> None:
    """Uninstall a plugin. Default: keep tables (just disable). --purge: drop tables."""
    db, mgr = _open()
    try:
        try:
            rec = mgr.info(plugin_id)
        except PluginNotFoundError as e:
            typer.echo(f"✘ {plugin_id} not installed")
            raise typer.Exit(2) from e

        if purge and not yes:
            tables = [t.name for t in rec.metadata.tables if t.purge_on_uninstall]
            typer.echo(f"将删除以下表（不可恢复）: {tables}")
            ok = questionary.confirm("确认 --purge?", default=False).ask()
            if not ok:
                typer.echo("Aborted.")
                raise typer.Exit(1)

        result = mgr.uninstall(plugin_id, purge=purge)
        action = "purged" if purge else "disabled"
        typer.echo(f"✔ {action}: {plugin_id} (dropped tables: {result['purged_tables']})")
    finally:
        db.close()


@app.command("upgrade")
def cmd_upgrade(path: Path = typer.Argument(...)) -> None:
    db, mgr = _open()
    try:
        try:
            rec = mgr.upgrade(path)
        except (PluginInstallError, PluginNotFoundError) as e:
            typer.echo(f"✘ Upgrade failed: {e}")
            raise typer.Exit(2) from e
        typer.echo(f"✔ upgraded: {rec.plugin_id} → v{rec.version}")
    finally:
        db.close()
