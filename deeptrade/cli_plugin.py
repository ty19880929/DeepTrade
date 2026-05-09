"""`deeptrade plugin` subcommand group."""

from __future__ import annotations

import questionary
import typer
import yaml
from rich.console import Console
from rich.table import Table

from deeptrade.core import paths
from deeptrade.core.db import Database
from deeptrade.core.github_fetch import (
    GitHubFetchError,
    NoMatchingReleaseError,
    TarballFetchError,
)
from deeptrade.core.plugin_manager import (
    PluginInstallError,
    PluginManager,
    PluginNotFoundError,
    UpgradeNoop,
    _load_metadata_yaml,
    summarize_for_install,
)
from deeptrade.core.plugin_source import (
    ResolvedSource,
    SourceResolveError,
    SourceResolver,
)
from deeptrade.core.registry import (
    RegistryClient,
    RegistryFetchError,
    RegistryNotFoundError,
    RegistrySchemaError,
)

app = typer.Typer(help="Install / manage plugins", no_args_is_help=True)

_RESOLVE_ERRORS: tuple[type[Exception], ...] = (
    RegistryNotFoundError,
    RegistryFetchError,
    RegistrySchemaError,
    NoMatchingReleaseError,
    TarballFetchError,
    GitHubFetchError,
    SourceResolveError,
)


def _open() -> tuple[Database, PluginManager]:
    db = Database(paths.db_path())
    return db, PluginManager(db)


def _format_origin(resolved: ResolvedSource) -> str:
    d = resolved.origin_detail
    if resolved.origin == "local":
        return f"本地路径 ({d.get('local_path', resolved.path)})"
    if resolved.origin == "github_registry":
        return (
            f"GitHub 注册表 ({d['repo']}@{d['ref']}, subdir={d['subdir']})"
        )
    if resolved.origin == "github_url":
        return f"GitHub URL ({d['repo']}@{d['ref']})"
    return resolved.origin


@app.command("install")
def cmd_install(
    source: str = typer.Argument(
        ..., help="短名（注册表）/ 本地路径 / GitHub URL"
    ),
    ref: str | None = typer.Option(
        None, "--ref", help="Tag / branch / sha (默认 = 该插件最新 release)"
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Install a plugin from the registry, a GitHub URL, or a local directory."""
    resolver = SourceResolver()
    try:
        resolved = resolver.resolve(source, ref)
    except _RESOLVE_ERRORS as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    try:
        try:
            meta = _load_metadata_yaml(resolved.path / "deeptrade_plugin.yaml")
        except PluginInstallError as e:
            typer.echo(f"✘ {e}")
            raise typer.Exit(2) from e

        typer.echo("─── 即将安装 ─────────────────────────────")
        typer.echo(f"来源: {_format_origin(resolved)}")
        typer.echo(summarize_for_install(meta, resolved.path))
        typer.echo("──────────────────────────────────────────")
        if not yes:
            ok = questionary.confirm("确认安装?", default=False).ask()
            if not ok:
                typer.echo("Aborted.")
                raise typer.Exit(1)

        db, mgr = _open()
        try:
            rec = mgr.install(resolved.path)
        except PluginInstallError as e:
            typer.echo(f"✘ Install failed: {e}")
            raise typer.Exit(2) from e
        finally:
            db.close()

        typer.echo(f"✔ 已安装: {rec.plugin_id} v{rec.version}")
    finally:
        if resolved.cleanup is not None:
            resolved.cleanup()


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
    """Show metadata for a plugin.

    If installed locally: shows the full installed metadata.yaml.
    If not installed: falls back to the registry entry (with an install hint).
    """
    db, mgr = _open()
    try:
        try:
            rec = mgr.info(plugin_id)
            typer.echo(
                yaml.safe_dump(rec.metadata.model_dump(mode="json"), allow_unicode=True)
            )
            return
        except PluginNotFoundError:
            pass  # fall through to registry lookup
    finally:
        db.close()

    try:
        entry = RegistryClient().resolve(plugin_id)
    except RegistryNotFoundError as e:
        typer.echo(f"✘ {plugin_id} 既未安装，也不在注册表中")
        raise typer.Exit(2) from e
    except (RegistryFetchError, RegistrySchemaError) as e:
        typer.echo(f"✘ {plugin_id} 未安装；查询注册表失败: {e}")
        raise typer.Exit(2) from e

    typer.echo(f"(未安装) {entry.plugin_id}")
    typer.echo(f"  name:        {entry.name}")
    typer.echo(f"  type:        {entry.type}")
    typer.echo(f"  description: {entry.description}")
    typer.echo(f"  repo:        {entry.repo}")
    typer.echo(f"  subdir:      {entry.subdir}")
    typer.echo(f"  install:     deeptrade plugin install {entry.plugin_id}")


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
def cmd_upgrade(
    source: str = typer.Argument(
        ..., help="短名（注册表）/ 本地路径 / GitHub URL"
    ),
    ref: str | None = typer.Option(
        None, "--ref", help="Tag / branch / sha (默认 = 该插件最新 release)"
    ),
) -> None:
    """Upgrade an installed plugin.

    Exit codes:
      0 — upgraded, or already at the candidate version
      2 — not installed / candidate < installed (downgrade forbidden) /
          network or registry failure
    """
    resolver = SourceResolver()
    try:
        resolved = resolver.resolve(source, ref)
    except _RESOLVE_ERRORS as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    try:
        db, mgr = _open()
        try:
            try:
                result = mgr.upgrade(resolved.path)
            except PluginNotFoundError as e:
                try:
                    meta = _load_metadata_yaml(resolved.path / "deeptrade_plugin.yaml")
                    pid = meta.plugin_id
                except PluginInstallError:
                    pid = source
                typer.echo(
                    f'✘ 插件 "{pid}" 未安装，请先执行 deeptrade plugin install'
                )
                raise typer.Exit(2) from e
            except PluginInstallError as e:
                typer.echo(f"✘ Upgrade failed: {e}")
                raise typer.Exit(2) from e

            if isinstance(result, UpgradeNoop):
                typer.echo(f"已是最新版本 v{result.version}")
                return
            typer.echo(f"✔ upgraded: {result.plugin_id} → v{result.version}")
        finally:
            db.close()
    finally:
        if resolved.cleanup is not None:
            resolved.cleanup()


@app.command("search")
def cmd_search(
    keyword: str | None = typer.Argument(
        None, help="可选过滤关键词（匹配 plugin_id / name / description）"
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="强制刷新注册表（旁路 ETag 缓存）"
    ),
) -> None:
    """List plugins available in the official registry."""
    try:
        registry = RegistryClient().fetch(force=no_cache)
    except (RegistryFetchError, RegistrySchemaError) as e:
        typer.echo(f"✘ {e}")
        raise typer.Exit(2) from e

    rows = list(registry.plugins.values())
    if keyword:
        kw = keyword.lower()
        rows = [
            entry
            for entry in rows
            if kw in entry.plugin_id.lower()
            or kw in entry.name.lower()
            or kw in entry.description.lower()
        ]

    if not rows:
        typer.echo("(no plugins matched)" if keyword else "(registry is empty)")
        return

    console = Console()
    table = Table(title="Available Plugins")
    table.add_column("plugin_id", style="cyan")
    table.add_column("name")
    table.add_column("type", style="green")
    table.add_column("description")
    for entry in sorted(rows, key=lambda x: x.plugin_id):
        table.add_row(entry.plugin_id, entry.name, entry.type, entry.description)
    console.print(table)
