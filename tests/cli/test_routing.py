"""Framework CLI routing tests — pure pass-through plugin dispatch.

Covers:
    * unknown command/plugin emits a helpful error including framework cmds
    * `data sync` is a stub (decision #4: temporarily disabled)
    * an installed plugin's argv is forwarded verbatim to its dispatch()
    * a disabled plugin produces an "enable first" hint
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.plugin_manager import PluginManager


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


def _install_fake_plugin(home: Path, plugin_id: str = "echo-plug") -> None:
    """Install a tiny plugin whose dispatch echoes argv to stdout."""
    pkg_name = plugin_id.replace("-", "_")
    table_name = f"{pkg_name}_log"
    src = home / f"_fake_src_{pkg_name}"
    src.mkdir()
    pkg = src / pkg_name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.py").write_text(
        "class EchoPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv):\n"
        "        import sys; sys.stdout.write('ECHO:' + ','.join(argv) + '\\n'); return 0\n"
    )
    mig_dir = src / "migrations"
    mig_dir.mkdir()
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER);\n"
    sql_path = mig_dir / "20260501_001_init.sql"
    sql_path.write_text(sql)
    import hashlib

    checksum = "sha256:" + hashlib.sha256(sql.encode()).hexdigest()

    yaml_text = (
        f"plugin_id: {plugin_id}\n"
        "name: Echo Plugin\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg_name}.plugin:EchoPlugin\n"
        "description: argv echo plugin used by tests\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        f'  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {table_name}\n"
        "    description: tiny test table\n"
        "    purge_on_uninstall: true\n"
    )
    (src / "deeptrade_plugin.yaml").write_text(yaml_text)
    # Use PluginManager directly (skip CLI confirm prompts)
    from deeptrade.core import paths

    db = Database(paths.db_path())
    try:
        PluginManager(db).install(src)
    finally:
        db.close()


def test_unknown_command_lists_framework_commands(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["nonesuch"])
    assert result.exit_code != 0
    out = result.output
    assert "未知命令或插件: 'nonesuch'" in out
    # Must mention all 4 framework commands so users know where to look.
    for cmd in ("config", "data", "init", "plugin"):
        assert cmd in out
    assert "plugin list" in out  # nudge to check installed plugins


def test_data_sync_stub_exits_with_message(home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["data", "sync"])
    assert result.exit_code == 2
    # The user-visible Chinese phrase is the primary token; the parenthetical
    # English label is kept for searchability in bug reports.
    assert "暂停使用" in result.output


def test_plugin_dispatch_forwards_argv_verbatim(home: Path) -> None:
    _install_fake_plugin(home)
    runner = CliRunner()
    result = runner.invoke(app, ["echo-plug", "run", "--force-sync", "x"])
    assert result.exit_code == 0
    assert "ECHO:run,--force-sync,x" in result.output


def test_disabled_plugin_emits_enable_hint(home: Path) -> None:
    _install_fake_plugin(home, plugin_id="muted-plug")
    from deeptrade.core import paths

    db = Database(paths.db_path())
    try:
        PluginManager(db).disable("muted-plug")
    finally:
        db.close()
    runner = CliRunner()
    result = runner.invoke(app, ["muted-plug", "run"])
    assert result.exit_code == 2
    assert "disabled" in result.output
    assert "plugin enable" in result.output


def test_help_excludes_plugin_subcommands(home: Path) -> None:
    """`deeptrade --help` lists framework commands only — installed plugins
    do NOT clutter top-level help (they live behind `plugin list`)."""
    _install_fake_plugin(home, plugin_id="hidden-plug")
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "hidden-plug" not in result.stdout
