"""DuckDB connection + migrations management.

Concurrency model (DESIGN §13.3): single-process single-writer connection
held by AppContext; writes are serialized on the runner main thread.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb

from deeptrade.core import paths

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{8}_\d{3,})_.+\.sql$")


class Database:
    """Thin wrapper around a single DuckDB connection.

    NOT thread-safe by design. The CLI is a single-process tool; if any future
    iteration introduces background workers, route writes through a queue
    consumed by the main thread (DESIGN §13.3).
    """

    def __init__(self, db_file: Path | None = None) -> None:
        self._path = db_file or paths.db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(str(self._path))
        # RLock (re-entrant) is required because transaction() acquires the lock
        # and the user code inside `with transaction()` calls execute() which
        # also acquires the lock. A plain Lock would self-deadlock.
        self._write_lock = threading.RLock()
        self._tx_depth = 0  # for reentrant transaction(); only outermost BEGIN/COMMIT

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    # --- query helpers -----------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> Any:
        with self._write_lock:
            if params is None:
                return self._conn.execute(sql)
            return self._conn.execute(sql, params)

    def fetchone(
        self, sql: str, params: tuple[Any, ...] | list[Any] | None = None
    ) -> tuple[Any, ...] | None:
        # Lock must span execute + fetch: duckdb's `_conn.execute()` returns the
        # connection itself with the result set attached. Releasing the lock
        # between execute and fetch lets another thread issue a new execute on
        # the same connection, overwriting our pending result and triggering a
        # native heap corruption (Windows 0xC0000374) on fetchone.
        with self._write_lock:
            if params is None:
                return self._conn.execute(sql).fetchone()
            return self._conn.execute(sql, params).fetchone()

    def fetchall(
        self, sql: str, params: tuple[Any, ...] | list[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        with self._write_lock:
            if params is None:
                return self._conn.execute(sql).fetchall()
            return self._conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Short transaction. Wraps BEGIN / COMMIT, rolls back on exception.

        Reentrant: nested ``with db.transaction():`` blocks do NOT start
        nested DuckDB transactions (which would error). Only the outermost
        block commits or rolls back. If any inner block raises, the
        outermost block sees the exception and rolls back.
        """
        with self._write_lock:
            outermost = self._tx_depth == 0
            if outermost:
                self._conn.execute("BEGIN")
            self._tx_depth += 1
            try:
                yield
                if outermost:
                    self._conn.execute("COMMIT")
            except Exception:
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _list_core_migrations() -> list[tuple[str, str]]:
    """Return [(version, sql_text), ...] sorted by version.

    Reads SQL files from the packaged ``deeptrade.core.migrations.core`` resource
    so it works whether installed as wheel or run from source.
    """
    pkg = resources.files("deeptrade.core.migrations.core")
    migrations: list[tuple[str, str]] = []
    for entry in pkg.iterdir():
        name = entry.name
        match = _MIGRATION_FILENAME_RE.match(name)
        if not match:
            continue
        version = match.group(1)
        migrations.append((version, entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda item: item[0])
    return migrations


def _applied_versions(db: Database) -> set[str]:
    # If schema_migrations doesn't exist yet (fresh DB), short-circuit.
    rows = db.fetchall(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name='schema_migrations'"
    )
    if not rows:
        return set()
    applied = db.fetchall("SELECT version FROM schema_migrations")
    return {row[0] for row in applied}


def apply_core_migrations(db: Database) -> list[str]:
    """Apply core migrations not yet recorded. Returns versions newly applied.

    After SQL migrations, runs idempotent data migrations (e.g. v0.6
    deepseek.* → llm.providers). Data migrations are idempotent by inspection
    of current state (not tracked in schema_migrations) so a re-run on a
    clean v0.6 DB is a no-op.
    """
    applied = _applied_versions(db)
    newly: list[str] = []
    for version, sql_text in _list_core_migrations():
        if version in applied:
            continue
        with db.transaction():
            db.execute(sql_text)
            db.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        newly.append(version)

    # v0.6 data migration — convert legacy deepseek.* config to llm.providers.
    from deeptrade.core.config_migrations import (
        migrate_legacy_deepseek_keys,
        migrate_legacy_deepseek_profile_key,
        migrate_llm_default_provider,
    )

    if migrate_legacy_deepseek_keys(db):
        newly.append("data:v06_llm_providers")
    # v0.7 data migration — rename deepseek.profile → app.profile.
    if migrate_legacy_deepseek_profile_key(db):
        newly.append("data:v07_app_profile")
    # v0.8 data migration — backfill is_default on existing providers.
    if migrate_llm_default_provider(db):
        newly.append("data:v08_llm_default_provider")
    return newly
