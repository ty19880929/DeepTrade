"""CLI tests for `deeptrade plugin` subcommands (PR-6).

Covers the new install/upgrade/search/info behaviors:
  - install / upgrade now accept short-name | URL | local path via SourceResolver
  - upgrade exit-code semantics: 0 (upgraded), 0 (already-latest), 2 (downgrade refused)
  - search prints registry rows; --no-cache forces a fresh fetch
  - info falls back to the registry when not installed locally
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.plugin_source import ResolvedSource
from deeptrade.core.registry import (
    Registry,
    RegistryEntry,
    RegistryFetchError,
    RegistryNotFoundError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


def _make_plugin_dir(base: Path, plugin_id: str, version: str) -> Path:
    pkg_name = plugin_id.replace("-", "_")
    src = base / f"_src_{plugin_id}_{version.replace('.', '_')}"
    src.mkdir(exist_ok=True)
    pkg = src / pkg_name
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    mig_dir = src / "migrations"
    mig_dir.mkdir(exist_ok=True)
    table_name = f"{pkg_name}_t"
    # Version-independent body so the same migration version keeps a stable
    # checksum across plugin-version bumps (T09 refuses checksum mutation
    # on historical migrations; the right pattern is to add a new migration
    # file, not edit an existing one).
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER);\n"
    (mig_dir / "20260501_001_init.sql").write_text(sql)
    checksum = "sha256:" + hashlib.sha256(sql.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: Test {plugin_id}\n"
        f"version: {version}\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {pkg_name}.plugin:TestPlugin\n"
        "description: tiny test plugin\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        '  - version: "20260501_001"\n'
        "    file: migrations/20260501_001_init.sql\n"
        f'    checksum: "{checksum}"\n'
        "tables:\n"
        f"  - name: {table_name}\n"
        "    description: test\n"
        "    purge_on_uninstall: true\n"
    )
    return src


def _entry(plugin_id: str = "limit-up-board") -> RegistryEntry:
    return RegistryEntry(
        plugin_id=plugin_id,
        name=f"Test {plugin_id}",
        type="strategy",
        description="A registered plugin",
        repo="ty19880929/DeepTradePluginOfficial",
        subdir=plugin_id.replace("-", "_"),
        tag_prefix=f"{plugin_id}/",
        min_framework_version="0.1.0",
    )


runner = CliRunner()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_local_path_happy_path(home: Path) -> None:
    src = _make_plugin_dir(home, "localplug", "0.1.0")
    result = runner.invoke(app, ["plugin", "install", str(src), "-y"])
    assert result.exit_code == 0, result.output
    assert "✔ 已安装: localplug v0.1.0" in result.output
    assert "本地路径" in result.output


def test_install_unknown_short_name_exits_2(home: Path) -> None:
    """If the short name is not in the registry, fail cleanly with exit 2."""
    fake_resolver = MagicMock()
    fake_resolver.resolve.side_effect = RegistryNotFoundError("plugin 'nope' not in registry")
    with patch("deeptrade.cli_plugin.SourceResolver", return_value=fake_resolver):
        result = runner.invoke(app, ["plugin", "install", "nope", "-y"])
    assert result.exit_code == 2
    assert "✘" in result.output
    assert "not in registry" in result.output


def test_install_calls_cleanup_on_success(home: Path) -> None:
    src = _make_plugin_dir(home, "cleanedup", "0.1.0")
    cleanup = MagicMock()
    fake_resolver = MagicMock()
    fake_resolver.resolve.return_value = ResolvedSource(
        path=src,
        origin="github_registry",
        origin_detail={
            "repo": "owner/repo",
            "ref": "cleanedup/v0.1.0",
            "subdir": "cleanedup",
        },
        cleanup=cleanup,
    )
    with patch("deeptrade.cli_plugin.SourceResolver", return_value=fake_resolver):
        result = runner.invoke(app, ["plugin", "install", "cleanedup", "-y"])
    assert result.exit_code == 0, result.output
    cleanup.assert_called_once()
    assert "GitHub 注册表" in result.output


def test_install_calls_cleanup_on_failure(home: Path) -> None:
    """Even if the install pipeline fails, cleanup() runs."""
    bad_src = home / "_bad"
    bad_src.mkdir()
    # Missing deeptrade_plugin.yaml → metadata load fails

    cleanup = MagicMock()
    fake_resolver = MagicMock()
    fake_resolver.resolve.return_value = ResolvedSource(
        path=bad_src,
        origin="local",
        origin_detail={"local_path": str(bad_src)},
        cleanup=cleanup,
    )
    with patch("deeptrade.cli_plugin.SourceResolver", return_value=fake_resolver):
        result = runner.invoke(app, ["plugin", "install", str(bad_src), "-y"])
    assert result.exit_code == 2
    cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def _install_local(home: Path, plugin_id: str, version: str) -> Path:
    src = _make_plugin_dir(home, plugin_id, version)
    result = runner.invoke(app, ["plugin", "install", str(src), "-y"])
    assert result.exit_code == 0, result.output
    return src


def test_upgrade_higher_version_exit_0(home: Path) -> None:
    _install_local(home, "vplug", "0.1.0")
    new_src = _make_plugin_dir(home, "vplug", "0.2.0")
    result = runner.invoke(app, ["plugin", "upgrade", str(new_src)])
    assert result.exit_code == 0, result.output
    assert "✔ 已升级: vplug → v0.2.0" in result.output


def test_upgrade_same_version_exits_0_with_already_latest_message(home: Path) -> None:
    _install_local(home, "vplug", "0.1.0")
    same_src = _make_plugin_dir(home, "vplug", "0.1.0")
    result = runner.invoke(app, ["plugin", "upgrade", str(same_src)])
    assert result.exit_code == 0, result.output
    assert "已是最新版本 v0.1.0" in result.output


def test_upgrade_lower_version_exits_2_with_uninstall_hint(home: Path) -> None:
    _install_local(home, "vplug", "0.1.0")
    runner.invoke(app, ["plugin", "upgrade", str(_make_plugin_dir(home, "vplug", "0.2.0"))])
    old_src = _make_plugin_dir(home, "vplug", "0.1.0")
    result = runner.invoke(app, ["plugin", "upgrade", str(old_src)])
    assert result.exit_code == 2
    assert "0.1.0" in result.output
    assert "0.2.0" in result.output
    assert "uninstall" in result.output


def test_upgrade_not_installed_exits_2(home: Path) -> None:
    src = _make_plugin_dir(home, "ghost", "0.1.0")
    result = runner.invoke(app, ["plugin", "upgrade", str(src)])
    assert result.exit_code == 2
    assert "未安装" in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_lists_all_plugins(home: Path) -> None:
    fake_registry = Registry(
        schema_version=1,
        plugins={
            "alpha-strategy": _entry("alpha-strategy"),
            "beta-helper": RegistryEntry(
                plugin_id="beta-helper",
                name="Beta Helper",
                type="strategy",
                description="A test helper plugin",
                repo="ty19880929/DeepTradePluginOfficial",
                subdir="beta",
                tag_prefix="beta-helper/",
                min_framework_version="0.1.0",
            ),
        },
    )
    fake_client = MagicMock()
    fake_client.fetch.return_value = fake_registry
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "search"])
    assert result.exit_code == 0, result.output
    assert "alpha-strategy" in result.output
    assert "beta-helper" in result.output


def test_search_filters_by_keyword(home: Path) -> None:
    fake_registry = Registry(
        schema_version=1,
        plugins={
            "alpha-strategy": _entry("alpha-strategy"),
            "beta-helper": RegistryEntry(
                plugin_id="beta-helper",
                name="Beta Helper",
                type="strategy",
                description="A test helper plugin",
                repo="x/y",
                subdir="beta",
                tag_prefix="beta-helper/",
                min_framework_version="0.1.0",
            ),
        },
    )
    fake_client = MagicMock()
    fake_client.fetch.return_value = fake_registry
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "search", "alpha"])
    assert result.exit_code == 0
    assert "alpha-strategy" in result.output
    assert "beta-helper" not in result.output


def test_search_no_match_message(home: Path) -> None:
    fake_registry = Registry(schema_version=1, plugins={"alpha-strategy": _entry("alpha-strategy")})
    fake_client = MagicMock()
    fake_client.fetch.return_value = fake_registry
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "search", "zzz"])
    assert result.exit_code == 0
    assert "未匹配到任何插件" in result.output


def test_search_no_cache_forces_fetch(home: Path) -> None:
    fake_registry = Registry(schema_version=1, plugins={"alpha-strategy": _entry("alpha-strategy")})
    fake_client = MagicMock()
    fake_client.fetch.return_value = fake_registry
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        runner.invoke(app, ["plugin", "search", "--no-cache"])
    fake_client.fetch.assert_called_once_with(force=True)


def test_search_network_error_exits_2(home: Path) -> None:
    fake_client = MagicMock()
    fake_client.fetch.side_effect = RegistryFetchError("DNS down")
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "search"])
    assert result.exit_code == 2
    assert "DNS down" in result.output


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_installed_dumps_yaml(home: Path) -> None:
    _install_local(home, "infoplug", "0.1.0")
    result = runner.invoke(app, ["plugin", "info", "infoplug"])
    assert result.exit_code == 0, result.output
    assert "plugin_id: infoplug" in result.output
    assert "version: 0.1.0" in result.output


def test_info_not_installed_falls_back_to_registry(home: Path) -> None:
    fake_client = MagicMock()
    fake_client.resolve.return_value = _entry("limit-up-board")
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "info", "limit-up-board"])
    assert result.exit_code == 0, result.output
    assert "(未安装)" in result.output
    assert "limit-up-board" in result.output
    assert "deeptrade plugin install limit-up-board" in result.output


def test_info_unknown_exits_2(home: Path) -> None:
    fake_client = MagicMock()
    fake_client.resolve.side_effect = RegistryNotFoundError("not found")
    with patch("deeptrade.cli_plugin.RegistryClient", return_value=fake_client):
        result = runner.invoke(app, ["plugin", "info", "ghost"])
    assert result.exit_code == 2
    assert "既未安装" in result.output


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


def test_enable_missing_install_path_message(home: Path) -> None:
    """T24 — `deeptrade plugin enable <pid>` must surface the manager's
    install_path-missing guidance rather than crashing with a raw
    PluginInstallError traceback.

    Repro: install a plugin, disable it, manually remove the install copy
    (simulating the state left by an older uninstall flow that wiped files
    but not the plugins row), then try to re-enable. The CLI should exit 2
    with a hint to reinstall — never enable the half-broken plugin only to
    have it crash on first invocation."""
    import shutil

    src = _make_plugin_dir(home, "broken-plug", "0.1.0")
    install_result = runner.invoke(app, ["plugin", "install", str(src), "-y"])
    assert install_result.exit_code == 0, install_result.output

    disable_result = runner.invoke(app, ["plugin", "disable", "broken-plug"])
    assert disable_result.exit_code == 0, disable_result.output

    # Wipe the install copy to simulate a missing install_path. DEEPTRADE_HOME
    # is set to tmp_path by the `home` fixture, so plugins live directly
    # under <home>/plugins/installed/<plugin_id>/<version>/.
    install_root = home / "plugins" / "installed" / "broken-plug"
    assert install_root.exists()
    shutil.rmtree(install_root)

    result = runner.invoke(app, ["plugin", "enable", "broken-plug"])
    assert result.exit_code == 2, result.output
    assert "install_path missing" in result.output
    assert "reinstall" in result.output
