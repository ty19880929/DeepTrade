"""V0.1 DoD — secret_store with keyring + plaintext fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.secrets import SecretStore


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    db = Database(tmp_path / "test.duckdb")
    apply_core_migrations(db)
    # force_plaintext=True ensures predictable behavior in tests regardless of host
    return SecretStore(db, force_plaintext=True)


# --- DoD 5: keyring round-trip (covered by force_plaintext=False path elsewhere) ---


def test_secret_store_plaintext_roundtrip(store: SecretStore) -> None:
    store.set("tushare.token", "abc123")
    assert store.get("tushare.token") == "abc123"

    records = store.list_records()
    assert any(r.key == "tushare.token" and r.method == "plaintext" for r in records)


# --- DoD 6: keyring unavailable → fallback to plaintext --------------------


def test_secret_store_fallback_to_plaintext_when_keyring_unavailable(store: SecretStore) -> None:
    """force_plaintext=True simulates `keyring not available`; expect the value
    is stored with method='plaintext'."""
    assert store.using_keyring is False
    store.set("deepseek.api_key", "sk-secret")
    assert store.get("deepseek.api_key") == "sk-secret"
    rec = next(r for r in store.list_records() if r.key == "deepseek.api_key")
    assert rec.method == "plaintext"


def test_secret_store_get_missing_returns_none(store: SecretStore) -> None:
    assert store.get("nonexistent") is None


def test_secret_store_set_overwrites(store: SecretStore) -> None:
    store.set("k", "v1")
    store.set("k", "v2")
    assert store.get("k") == "v2"


def test_secret_store_delete(store: SecretStore) -> None:
    store.set("k", "v")
    store.delete("k")
    assert store.get("k") is None


def test_secret_store_keyring_path_when_available(tmp_path: Path) -> None:
    """If keyring probe succeeds, set() should NOT write the value into
    secret_store.encrypted_value (stays empty)."""

    class FakeKeyring:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, user: str) -> str | None:
            return self.store.get((service, user))

        def set_password(self, service: str, user: str, password: str) -> None:
            self.store[(service, user)] = password

        def delete_password(self, service: str, user: str) -> None:
            self.store.pop((service, user), None)

    db = Database(tmp_path / "test.duckdb")
    apply_core_migrations(db)
    s = SecretStore(db)
    fake = FakeKeyring()
    s._keyring = fake  # type: ignore[assignment]   # inject for test

    s.set("k", "secret-value")
    rec = next(r for r in s.list_records() if r.key == "k")
    assert rec.method == "keyring"
    assert rec.value == ""  # plaintext column not used when keyring stores it
    assert s.get("k") == "secret-value"
    assert fake.store[("deeptrade", "k")] == "secret-value"
