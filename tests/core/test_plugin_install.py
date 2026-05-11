"""Plugin install pipeline tests — reserved-word guard + migration isolation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.plugin_manager import (
    RESERVED_PLUGIN_IDS,
    PluginInstallError,
    PluginManager,
)


def _make_minimal_plugin_dir(
    base: Path,
    plugin_id: str,
    *,
    table_ddl: str = "CREATE TABLE IF NOT EXISTS test_table (id INTEGER);\n",
    table_name: str = "test_table",
) -> Path:
    """Synthesize a minimal but valid plugin source dir under ``base``."""
    src = base / f"_src_{plugin_id}"
    src.mkdir()
    pkg = src / plugin_id.replace("-", "_")
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "plugin.py").write_text(
        "class TestPlugin:\n"
        "    metadata = None\n"
        "    def validate_static(self, ctx): pass\n"
        "    def dispatch(self, argv): return 0\n"
    )
    mig_dir = src / "migrations"
    mig_dir.mkdir()
    (mig_dir / "20260501_001_init.sql").write_text(table_ddl)
    checksum = "sha256:" + hashlib.sha256(table_ddl.encode()).hexdigest()
    (src / "deeptrade_plugin.yaml").write_text(
        f"plugin_id: {plugin_id}\n"
        f"name: Test Plugin {plugin_id}\n"
        "version: 0.1.0\n"
        "type: strategy\n"
        'api_version: "1"\n'
        f"entrypoint: {plugin_id.replace('-', '_')}.plugin:TestPlugin\n"
        "description: minimal test plugin\n"
        "permissions:\n  llm: false\n  llm_tools: false\n"
        "migrations:\n"
        f'  - version: "20260501_001"\n'
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


# ---------------------------------------------------------------------------
# Reserved-word guard
# ---------------------------------------------------------------------------


def test_reserved_words_are_those_documented() -> None:
    """RESERVED_PLUGIN_IDS must equal the framework command set."""
    assert RESERVED_PLUGIN_IDS == frozenset({"init", "config", "plugin", "data"})


@pytest.mark.parametrize("reserved", sorted(RESERVED_PLUGIN_IDS))
def test_install_rejects_reserved_plugin_id(home: Path, reserved: str) -> None:
    """A plugin colliding with a framework command must be rejected at install."""
    src = _make_minimal_plugin_dir(home, reserved)
    db = Database(home / "deeptrade.duckdb")
    try:
        with pytest.raises(PluginInstallError, match="reserved"):
            PluginManager(db).install(src)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Migration isolation: framework migrations don't include plugin tables
# ---------------------------------------------------------------------------


def test_framework_migration_creates_no_business_tables(home: Path) -> None:
    """Fresh init must NOT create stock_basic / daily / strategy_runs etc.
    Those are plugin-owned (Plan A pure isolation) — the framework only manages
    its own tables."""
    db = Database(home / "deeptrade.duckdb")
    try:
        tables = {
            r[0]
            for r in db.fetchall(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            )
        }
    finally:
        db.close()
    forbidden = {
        "stock_basic",
        "trade_cal",
        "daily",
        "daily_basic",
        "moneyflow",
        "strategy_runs",
        "strategy_events",
    }
    leak = tables & forbidden
    assert not leak, f"framework migration leaked plugin-domain tables: {leak}"


def test_plugin_migration_lands_in_plugin_schema_migrations(home: Path) -> None:
    """Installing a plugin records its migration in plugin_schema_migrations,
    NOT in the framework schema_migrations."""
    src = _make_minimal_plugin_dir(home, "iso-plug")
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src)
        framework_versions = {r[0] for r in db.fetchall("SELECT version FROM schema_migrations")}
        plugin_versions = {
            (r[0], r[1])
            for r in db.fetchall("SELECT plugin_id, version FROM plugin_schema_migrations")
        }
    finally:
        db.close()
    # Plugin's version is in plugin_schema_migrations, NOT schema_migrations.
    assert ("iso-plug", "20260501_001") in plugin_versions
    assert "20260501_001" not in framework_versions


def test_plugin_uninstall_purge_drops_plugin_tables_only(home: Path) -> None:
    """`uninstall --purge` drops the plugin's tables but leaves framework tables intact."""
    src = _make_minimal_plugin_dir(
        home,
        "purge-plug",
        table_ddl="CREATE TABLE IF NOT EXISTS purge_plug_table (id INTEGER);\n",
        table_name="purge_plug_table",
    )
    db = Database(home / "deeptrade.duckdb")
    try:
        PluginManager(db).install(src)
        # Sanity: plugin table exists, framework table exists
        assert db.fetchone(
            "SELECT 1 FROM information_schema.tables WHERE table_name='purge_plug_table'"
        )
        assert db.fetchone("SELECT 1 FROM information_schema.tables WHERE table_name='app_config'")

        PluginManager(db).uninstall("purge-plug", purge=True)

        # Plugin table gone; framework table still here
        assert not db.fetchone(
            "SELECT 1 FROM information_schema.tables WHERE table_name='purge_plug_table'"
        )
        assert db.fetchone("SELECT 1 FROM information_schema.tables WHERE table_name='app_config'")
    finally:
        db.close()
