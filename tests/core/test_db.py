"""Core schema invariants — framework-only tables (Plan A pure isolation).

After the v0.5 reshape the framework owns ONLY:
    app_config, secret_store, schema_migrations,
    plugins, plugin_tables, plugin_schema_migrations,
    llm_calls, tushare_sync_state, tushare_calls

All business / strategy data is plugin-owned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeptrade.cli import app
from deeptrade.core.db import Database, apply_core_migrations


@pytest.fixture
def fresh_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.duckdb")


# --- init creates DB + dirs ----------------------------------------------


def test_init_creates_db_file_and_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompts"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "deeptrade.duckdb").is_file()
    assert (tmp_path / "logs").is_dir()
    assert "Database created" in result.stdout
    assert "Schema applied" in result.stdout


def test_init_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-prompts"])
    result = runner.invoke(app, ["init", "--no-prompts"])
    assert result.exit_code == 0
    assert "already initialized" in result.stdout


# --- migrations record version --------------------------------------------


def test_apply_core_migrations_records_version(fresh_db: Database) -> None:
    """v0.7 — two core SQL migrations: init + drop_llm_calls_stage. Framework owns
    no business tables."""
    applied = apply_core_migrations(fresh_db)
    assert applied == ["20260427_001", "20260501_002"]
    rows = fresh_db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
    assert tuple(rows) == (("20260427_001",), ("20260501_002",))


def test_apply_core_migrations_skips_applied_versions(fresh_db: Database) -> None:
    apply_core_migrations(fresh_db)
    second = apply_core_migrations(fresh_db)
    assert second == []


# --- framework tables exist; business tables do NOT -----------------------


def test_framework_owns_only_minimal_table_set(fresh_db: Database) -> None:
    apply_core_migrations(fresh_db)
    tables = {
        r[0]
        for r in fresh_db.fetchall(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        )
    }
    expected = {
        "app_config",
        "secret_store",
        "schema_migrations",
        "plugins",
        "plugin_tables",
        "plugin_schema_migrations",
        "llm_calls",
        "tushare_sync_state",
        "tushare_calls",
    }
    assert tables == expected, f"unexpected drift: {tables - expected} missing: {expected - tables}"


# --- tushare_sync_state has plugin_id in PK (Plan A pure isolation) -------


def test_tushare_sync_state_has_plugin_id_column_and_pk(fresh_db: Database) -> None:
    apply_core_migrations(fresh_db)
    cols = [
        r[0]
        for r in fresh_db.fetchall(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='tushare_sync_state' ORDER BY ordinal_position"
        )
    ]
    # plugin_id is the first PK column
    assert cols[0] == "plugin_id"
    assert "data_completeness" in cols


def test_tushare_sync_state_default_data_completeness(fresh_db: Database) -> None:
    apply_core_migrations(fresh_db)
    fresh_db.execute(
        "INSERT INTO tushare_sync_state(plugin_id, api_name, trade_date, status) "
        "VALUES (?, ?, ?, ?)",
        ("test-plugin", "stock_basic", "*", "ok"),
    )
    row = fresh_db.fetchone(
        "SELECT data_completeness FROM tushare_sync_state WHERE api_name='stock_basic'"
    )
    assert row is not None and row[0] == "final"


def test_tushare_calls_has_plugin_id_column(fresh_db: Database) -> None:
    apply_core_migrations(fresh_db)
    cols = {
        r[0]
        for r in fresh_db.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tushare_calls'"
        )
    }
    assert "plugin_id" in cols
