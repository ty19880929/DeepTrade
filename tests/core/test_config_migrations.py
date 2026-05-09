"""v0.6 — legacy deepseek.* → llm.providers / llm.<name>.api_key migration.
v0.7 — deepseek.profile → app.profile migration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeptrade.core.config import ConfigService
from deeptrade.core.config_migrations import (
    migrate_legacy_deepseek_keys,
    migrate_legacy_deepseek_profile_key,
    migrate_llm_default_provider,
)
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.secrets import SecretStore


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.duckdb")
    apply_core_migrations(d)
    return d


def _seed_legacy(db: Database, *, with_api_key: bool = True) -> None:
    """Inject pre-v0.6 deepseek.* rows directly so we can simulate an upgrade."""
    rows = [
        ("deepseek.base_url", json.dumps("https://api.deepseek.com")),
        ("deepseek.model", json.dumps("deepseek-v4-pro")),
        ("deepseek.timeout", json.dumps(180)),
        ("deepseek.audit_full_payload", json.dumps(False)),
    ]
    # First, scrub any v0.6 rows that may have been planted by an earlier run.
    db.execute("DELETE FROM app_config WHERE key IN ('llm.providers', 'llm.audit_full_payload')")
    for key, value in rows:
        db.execute("DELETE FROM app_config WHERE key = ?", (key,))
        db.execute(
            "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
            (key, value, False),
        )
    if with_api_key:
        store = SecretStore(db, force_plaintext=True)
        store.set("deepseek.api_key", "sk-legacy-token")


# ---------------------------------------------------------------------------


def test_migration_creates_llm_providers_dict(db: Database) -> None:
    _seed_legacy(db)
    migrated = migrate_legacy_deepseek_keys(db)
    assert migrated is True

    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    cfg = svc.get_app_config()
    assert "deepseek" in cfg.llm_providers
    p = cfg.llm_providers["deepseek"]
    assert p.base_url == "https://api.deepseek.com"
    assert p.model == "deepseek-v4-pro"
    assert p.timeout == 180


def test_migration_renames_secret_api_key(db: Database) -> None:
    _seed_legacy(db, with_api_key=True)
    migrate_legacy_deepseek_keys(db)
    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    # Old secret-store key gone; new one carries the same value.
    assert svc.get("llm.deepseek.api_key") == "sk-legacy-token"
    rows = db.fetchall("SELECT 1 FROM secret_store WHERE key = 'deepseek.api_key'")
    assert rows == []


def test_migration_deletes_legacy_app_config_keys(db: Database) -> None:
    _seed_legacy(db)
    migrate_legacy_deepseek_keys(db)
    rows = db.fetchall(
        "SELECT key FROM app_config WHERE key LIKE 'deepseek.%'"
    )
    # ``deepseek.profile`` migration is handled by a separate function in v0.7;
    # seed didn't write it so this should be empty either way.
    assert rows == []


def test_migration_copies_audit_full_payload(db: Database) -> None:
    _seed_legacy(db)
    # Override the seeded value to make sure the migration faithfully copies True
    db.execute(
        "UPDATE app_config SET value_json = ? WHERE key = 'deepseek.audit_full_payload'",
        (json.dumps(True),),
    )
    migrate_legacy_deepseek_keys(db)
    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    assert svc.get_app_config().llm_audit_full_payload is True


def test_migration_is_idempotent(db: Database) -> None:
    _seed_legacy(db)
    assert migrate_legacy_deepseek_keys(db) is True
    # Second run — no legacy keys left, llm.providers already present
    assert migrate_legacy_deepseek_keys(db) is False


def test_migration_no_op_on_fresh_db(db: Database) -> None:
    """A pristine v0.6 install (no legacy rows) must not write llm.providers."""
    assert migrate_legacy_deepseek_keys(db) is False
    rows = db.fetchall("SELECT 1 FROM app_config WHERE key = 'llm.providers'")
    assert rows == []


def test_migration_handles_partial_legacy_state(db: Database) -> None:
    """Edge case: user only ever set deepseek.api_key (not base_url/model/timeout).
    Migration should still produce a usable provider entry using AppConfig defaults.
    """
    store = SecretStore(db, force_plaintext=True)
    store.set("deepseek.api_key", "sk-only-key")
    # Seed only the audit flag so legacy_rows is non-empty (the "fresh install"
    # short-circuit otherwise correctly skips).
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("deepseek.audit_full_payload", json.dumps(False), False),
    )

    assert migrate_legacy_deepseek_keys(db) is True

    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    cfg = svc.get_app_config()
    assert "deepseek" in cfg.llm_providers
    assert cfg.llm_providers["deepseek"].base_url == "https://api.deepseek.com"
    assert svc.get("llm.deepseek.api_key") == "sk-only-key"


# ---------------------------------------------------------------------------
# v0.7 — deepseek.profile → app.profile
# ---------------------------------------------------------------------------


def test_profile_key_migration_renames_row(db: Database) -> None:
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("deepseek.profile", json.dumps("quality"), False),
    )
    assert migrate_legacy_deepseek_profile_key(db) is True
    # Old row gone, new row carries the same value.
    assert db.fetchone("SELECT 1 FROM app_config WHERE key = 'deepseek.profile'") is None
    row = db.fetchone("SELECT value_json FROM app_config WHERE key = 'app.profile'")
    assert row is not None
    assert json.loads(row[0]) == "quality"


def test_profile_key_migration_idempotent(db: Database) -> None:
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("deepseek.profile", json.dumps("fast"), False),
    )
    assert migrate_legacy_deepseek_profile_key(db) is True
    assert migrate_legacy_deepseek_profile_key(db) is False


def test_profile_key_migration_no_op_on_fresh_db(db: Database) -> None:
    assert migrate_legacy_deepseek_profile_key(db) is False
    assert db.fetchone("SELECT 1 FROM app_config WHERE key = 'app.profile'") is None


def test_profile_key_migration_skips_when_new_already_set(db: Database) -> None:
    """User created v0.7 fresh AND has a stale legacy row from manual editing —
    keep the v0.7 value as canonical, do not overwrite."""
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("app.profile", json.dumps("fast"), False),
    )
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("deepseek.profile", json.dumps("quality"), False),
    )
    assert migrate_legacy_deepseek_profile_key(db) is False
    # Legacy row left untouched (cleanup is the user's job — they explicitly set both)
    row = db.fetchone("SELECT value_json FROM app_config WHERE key = 'app.profile'")
    assert row is not None
    assert json.loads(row[0]) == "fast"


# ---------------------------------------------------------------------------
# v0.8 — backfill is_default on existing llm.providers rows.
# ---------------------------------------------------------------------------


def _seed_providers(db: Database, providers: dict) -> None:
    db.execute("DELETE FROM app_config WHERE key = 'llm.providers'")
    db.execute(
        "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
        ("llm.providers", json.dumps(providers), False),
    )


def test_legacy_deepseek_migration_marks_default(db: Database) -> None:
    """v0.6 legacy migration must produce a deepseek entry with is_default=True
    so the v0.8 invariant holds out of the box."""
    _seed_legacy(db)
    assert migrate_legacy_deepseek_keys(db) is True
    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    assert svc.get_default_llm_provider() == "deepseek"


def test_default_provider_migration_promotes_first(db: Database) -> None:
    """A pre-v0.8 dict (no is_default field anywhere) gets the first key
    promoted to default."""
    _seed_providers(
        db,
        {
            "kimi": {"base_url": "u1", "model": "m1", "timeout": 180},
            "deepseek": {"base_url": "u2", "model": "m2", "timeout": 180},
        },
    )
    assert migrate_llm_default_provider(db) is True
    svc = ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))
    assert svc.get_default_llm_provider() == "kimi"


def test_default_provider_migration_idempotent_when_default_exists(db: Database) -> None:
    _seed_providers(
        db,
        {
            "deepseek": {"base_url": "u", "model": "m", "timeout": 180, "is_default": True},
            "kimi": {"base_url": "u", "model": "m", "timeout": 180, "is_default": False},
        },
    )
    assert migrate_llm_default_provider(db) is False


def test_default_provider_migration_no_op_on_empty(db: Database) -> None:
    """No llm.providers row at all and an empty providers dict are both
    no-ops — nothing to backfill."""
    assert migrate_llm_default_provider(db) is False
    _seed_providers(db, {})
    assert migrate_llm_default_provider(db) is False
