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
    # auto_migrate=False keeps the "fresh" semantics tests depend on: each
    # test exercises apply_core_migrations explicitly to assert what it does.
    return Database(tmp_path / "test.duckdb", auto_migrate=False)


# --- init creates DB + dirs ----------------------------------------------


def test_init_creates_db_file_and_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompts"])
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "deeptrade.duckdb").is_file()
    assert (tmp_path / "logs").is_dir()


def test_init_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPTRADE_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-prompts"])
    result = runner.invoke(app, ["init", "--no-prompts"])
    assert result.exit_code == 0


# --- migrations record version --------------------------------------------


def test_apply_core_migrations_records_version(fresh_db: Database) -> None:
    """Framework owns no business tables."""
    applied = apply_core_migrations(fresh_db)
    assert applied == ["20260509_001", "20260512_001"]
    rows = fresh_db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
    assert tuple(rows) == (("20260509_001",), ("20260512_001",))


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


# --- Database.__init__ auto-migrate (v0.4.2) -----------------------------


def test_database_init_runs_pending_migrations(tmp_path: Path) -> None:
    """A fresh Database() with default auto_migrate=True applies every core
    migration on first open — no separate `db upgrade` call needed."""
    db = Database(tmp_path / "auto.duckdb")
    try:
        rows = db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        assert tuple(rows) == (("20260509_001",), ("20260512_001",))
    finally:
        db.close()


def test_database_init_auto_migrate_false_skips(tmp_path: Path) -> None:
    """auto_migrate=False is the documented opt-out for the CLI's
    `db init` / `db upgrade` commands that want to collect the applied
    list themselves."""
    db = Database(tmp_path / "manual.duckdb", auto_migrate=False)
    try:
        rows = db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='schema_migrations'"
        )
        assert rows == []
    finally:
        db.close()


def test_database_init_env_var_skips_auto_migrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEEPTRADE_SKIP_AUTO_MIGRATE=1 is the recovery escape hatch."""
    monkeypatch.setenv("DEEPTRADE_SKIP_AUTO_MIGRATE", "1")
    db = Database(tmp_path / "skip.duckdb")
    try:
        rows = db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='schema_migrations'"
        )
        assert rows == []
    finally:
        db.close()


def test_database_init_migration_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing migration must bubble out of Database() as a hard error so
    callers can't proceed against a half-migrated schema."""
    from deeptrade.core import db as db_module

    boom = RuntimeError("synthetic migration failure")

    def _failing(_db: Database) -> list[str]:
        raise boom

    monkeypatch.setattr(db_module, "apply_core_migrations", _failing)

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        Database(tmp_path / "broken.duckdb")


def test_legacy_tushare_cache_wiped_on_first_open(tmp_path: Path) -> None:
    """v0.4.1 introduced a wrapped tushare cache payload format and a
    migration that drops the legacy table. Auto-migrate must apply that
    migration on first open so a pre-0.4.1 DB doesn't poison the v0.4.1+
    reader. Regression-locks the TypeError reported against
    `deeptrade limit-up-board lgb train`.
    """
    db_file = tmp_path / "legacy.duckdb"

    # Stage 1: simulate a pre-0.4.1 DB — only the very first migration is on
    # record, and the legacy cache table has a bare-array payload row.
    db = Database(db_file, auto_migrate=False)
    try:
        # Apply only the v0.4.0 migration (init schema) by name; intentionally
        # leave the v0.4.1 drop migration unrecorded.
        from deeptrade.core.db import _list_core_migrations

        for version, sql_text in _list_core_migrations():
            if version != "20260509_001":
                continue
            with db.transaction():
                db.execute(sql_text)
                db.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (version,),
                )
        db.execute(
            "CREATE TABLE tushare_cache_blob ("
            "  plugin_id VARCHAR NOT NULL,"
            "  api_name VARCHAR NOT NULL,"
            "  trade_date VARCHAR NOT NULL,"
            "  params_hash VARCHAR NOT NULL,"
            "  payload_json VARCHAR NOT NULL,"
            "  cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "  PRIMARY KEY (plugin_id, api_name, trade_date, params_hash)"
            ")"
        )
        db.execute(
            "INSERT INTO tushare_cache_blob(plugin_id, api_name, trade_date, "
            "params_hash, payload_json) VALUES (?, ?, ?, ?, ?)",
            ("legacy-plugin", "trade_cal", "*", "0" * 64, '[{"cal_date":"20260101"}]'),
        )
    finally:
        db.close()

    # Stage 2: re-open with auto_migrate=True → the drop migration must run
    # and the legacy table must be gone.
    db = Database(db_file)
    try:
        rows = db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='tushare_cache_blob'"
        )
        assert rows == [], "legacy tushare_cache_blob should be dropped by auto-migrate"
        applied = {r[0] for r in db.fetchall("SELECT version FROM schema_migrations")}
        assert "20260512_001" in applied
    finally:
        db.close()
