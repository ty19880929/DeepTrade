"""PluginManager.upgrade — version comparison semantics (PR-6).

- candidate == installed → UpgradeNoop
- candidate > installed  → run (covered indirectly by other tests)
- candidate < installed  → PluginInstallError with the Chinese guidance
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.plugin_manager import (
    PluginInstallError,
    PluginManager,
    PluginNotFoundError,
    UpgradeNoop,
)


def _make_plugin_dir(base: Path, plugin_id: str, version: str) -> Path:
    """Tiny but valid plugin source. The migration body is version-independent
    so the same migration version carries the same checksum across plugin
    versions — the realistic upgrade pattern is "add a new migration file",
    not "mutate the existing one" (the latter is what T09 refuses)."""
    pkg_name = plugin_id.replace("-", "_")
    # _make_plugin_dir may be called multiple times with the same (id, version)
    # in a single test (e.g. install 0.1.0, upgrade 0.2.0, then "downgrade" to
    # 0.1.0). exist_ok=True makes this idempotent.
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


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    db = Database(tmp_path / "deeptrade.duckdb")
    apply_core_migrations(db)
    db.close()
    return tmp_path


@pytest.fixture
def installed_v010(home: Path) -> tuple[PluginManager, Database]:
    db = Database(home / "deeptrade.duckdb")
    mgr = PluginManager(db)
    src = _make_plugin_dir(home, "verplug", "0.1.0")
    mgr.install(src)
    return mgr, db


def test_upgrade_same_version_returns_noop(
    installed_v010: tuple[PluginManager, Database], home: Path
) -> None:
    mgr, db = installed_v010
    try:
        same_src = _make_plugin_dir(home, "verplug", "0.1.0")
        result = mgr.upgrade(same_src)
        assert isinstance(result, UpgradeNoop)
        assert result.plugin_id == "verplug"
        assert result.version == "0.1.0"
    finally:
        db.close()


def test_upgrade_higher_version_runs(
    installed_v010: tuple[PluginManager, Database], home: Path
) -> None:
    mgr, db = installed_v010
    try:
        new_src = _make_plugin_dir(home, "verplug", "0.2.0")
        result = mgr.upgrade(new_src)
        assert not isinstance(result, UpgradeNoop)
        assert result.plugin_id == "verplug"
        assert result.version == "0.2.0"
    finally:
        db.close()


def test_upgrade_lower_version_refuses_with_uninstall_hint(
    installed_v010: tuple[PluginManager, Database], home: Path
) -> None:
    mgr, db = installed_v010
    try:
        # First bump to 0.2.0 so that 0.1.0 becomes a downgrade candidate
        mgr.upgrade(_make_plugin_dir(home, "verplug", "0.2.0"))
        old_src = _make_plugin_dir(home, "verplug", "0.1.0")
        with pytest.raises(PluginInstallError) as excinfo:
            mgr.upgrade(old_src)
        msg = str(excinfo.value)
        assert "0.1.0" in msg
        assert "0.2.0" in msg
        assert "uninstall" in msg
        assert "purge" in msg
    finally:
        db.close()


def test_upgrade_not_installed_raises_not_found(home: Path) -> None:
    db = Database(home / "deeptrade.duckdb")
    try:
        mgr = PluginManager(db)
        src = _make_plugin_dir(home, "neverplug", "0.1.0")
        with pytest.raises(PluginNotFoundError):
            mgr.upgrade(src)
    finally:
        db.close()
