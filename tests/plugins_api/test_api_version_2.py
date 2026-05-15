"""v0.6 H4 — api_version "2" dispatch(ctx, argv) + public service exports.

Locks the two-version dispatch contract (1 and 2 both first-class) and
asserts the new public surface exports are importable directly from
``deeptrade.plugins_api`` so plugin authors stop reaching into
``deeptrade.core.*``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app
from deeptrade.core.db import Database, apply_core_migrations


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


def _install_plugin(home: Path, *, api_version: str, plugin_id: str) -> Path:
    """Install a plugin whose dispatch echoes the arity it received.

    ``api_version="1"`` plugins echo ``V1:<argv>``; ``"2"`` plugins echo
    ``V2:<plugin_id>:<argv>`` so we can prove the framework hands them the
    PluginContext."""
    pkg = plugin_id.replace("-", "_")
    src = home / f"_src_{plugin_id}"
    src.mkdir()
    (src / pkg).mkdir()
    (src / pkg / "__init__.py").write_text("")
    if api_version == "2":
        body = (
            "class EchoPlugin:\n"
            "    metadata = None\n"
            "    def validate_static(self, ctx): pass\n"
            "    def dispatch(self, ctx, argv):\n"
            "        import sys\n"
            "        sys.stdout.write(f'V2:{ctx.plugin_id}:{\",\".join(argv)}\\n')\n"
            "        return 0\n"
        )
    else:
        body = (
            "class EchoPlugin:\n"
            "    metadata = None\n"
            "    def validate_static(self, ctx): pass\n"
            "    def dispatch(self, argv):\n"
            "        import sys\n"
            "        sys.stdout.write(f'V1:{\",\".join(argv)}\\n')\n"
            "        return 0\n"
        )
    (src / pkg / "plugin.py").write_text(body)
    table_name = f"{pkg}_t"
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER);\n"
    (src / "migrations").mkdir()
    (src / "migrations" / "20260501_001_init.sql").write_text(sql)
    checksum = "sha256:" + hashlib.sha256(sql.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: Echo {plugin_id}\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        f'api_version: "{api_version}"\n'
        f"entrypoint: {pkg}.plugin:EchoPlugin\n"
        "description: dispatch arity probe\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {table_name}\n    description: t\n    purge_on_uninstall: true\n"
    )
    return src


# ---------------------------------------------------------------------------
# H4 — both api_versions accepted at install
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_version", ["1", "2"])
def test_install_accepts_both_api_versions(home: Path, api_version: str) -> None:
    from deeptrade.core.plugin_manager import PluginManager

    src = _install_plugin(home, api_version=api_version, plugin_id=f"v{api_version}plug")
    db = Database(home / "deeptrade.duckdb")
    try:
        rec = PluginManager(db).install(src, install_deps=False)
        assert rec.api_version == api_version
    finally:
        db.close()


def test_install_rejects_unknown_api_version(home: Path) -> None:
    """``api_version`` outside the supported set must fail with a clear
    pointer to the allowed values."""
    from deeptrade.core.plugin_manager import PluginInstallError, PluginManager

    src = _install_plugin(home, api_version="2", plugin_id="hostile-plug")
    # Mutate the yaml to claim an unknown api_version after the file is on
    # disk so we exercise the install-pipeline check, not parse-time only.
    yaml_text = (src / "deeptrade_plugin.yaml").read_text()
    (src / "deeptrade_plugin.yaml").write_text(
        yaml_text.replace('api_version: "2"', 'api_version: "99"')
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="not supported by framework"):
            PluginManager(db).install(src, install_deps=False)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# H4 — dispatch arity is selected by api_version
# ---------------------------------------------------------------------------


def _do_install(home: Path, src: Path) -> None:
    from deeptrade.core.plugin_manager import PluginManager

    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src, install_deps=False)
    finally:
        db.close()


def test_dispatch_v1_receives_argv_only(home: Path) -> None:
    """``api_version="1"`` plugins keep the legacy single-argument
    dispatch signature."""
    src = _install_plugin(home, api_version="1", plugin_id="legacy-plug")
    _do_install(home, src)
    runner = CliRunner()
    result = runner.invoke(app, ["legacy-plug", "run", "--force-sync"])
    assert result.exit_code == 0, result.output
    assert "V1:run,--force-sync" in result.output


def test_dispatch_v2_receives_ctx_then_argv(home: Path) -> None:
    """``api_version="2"`` plugins get ``dispatch(ctx, argv)``; the test
    plugin echoes ``ctx.plugin_id`` so we know the framework actually
    constructed and passed a PluginContext."""
    src = _install_plugin(home, api_version="2", plugin_id="modern-plug")
    _do_install(home, src)
    runner = CliRunner()
    result = runner.invoke(app, ["modern-plug", "run", "--force-sync"])
    assert result.exit_code == 0, result.output
    assert "V2:modern-plug:run,--force-sync" in result.output


# ---------------------------------------------------------------------------
# H4-b — plugins_api re-exports LLMManager + TushareClient
# ---------------------------------------------------------------------------


def test_plugins_api_exposes_llm_manager_and_tushare_client() -> None:
    """Both classes must be importable from the public surface so plugin
    authors don't have to ``from deeptrade.core.* import ...`` internal
    paths. The class identity must match the canonical core implementation
    (we re-export, not rewrap)."""
    import deeptrade.plugins_api as api
    from deeptrade.core.llm_manager import LLMManager as CoreLLMManager
    from deeptrade.core.tushare_client import TushareClient as CoreTushareClient

    assert api.LLMManager is CoreLLMManager
    assert api.TushareClient is CoreTushareClient
    assert "LLMManager" in api.__all__
    assert "TushareClient" in api.__all__
