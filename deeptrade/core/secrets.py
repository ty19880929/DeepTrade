"""Secret storage with keyring preference + plaintext fallback.

DESIGN §7.1: keyring is the default; if unavailable (headless Linux, CI, some
Docker images), fall back to plaintext-in-DuckDB with an EXPLICIT warning so
the user knows the risk.

The encryption_method column on secret_store distinguishes the two paths so
``deeptrade config show`` can warn when any secret is plaintext-stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from deeptrade.core.db import Database

logger = logging.getLogger(__name__)

# keyring service name namespace
_KR_SERVICE = "deeptrade"


class _KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


def _try_load_keyring() -> _KeyringBackend | None:
    """Attempt to import & probe keyring. Return backend or None on failure."""
    try:
        import keyring  # noqa: PLC0415 — deferred import on purpose
        from keyring.errors import KeyringError  # noqa: PLC0415
    except ImportError:
        return None
    try:
        # Probe: round-trip a sentinel
        keyring.set_password(_KR_SERVICE, "__probe__", "ok")
        if keyring.get_password(_KR_SERVICE, "__probe__") != "ok":
            return None
        keyring.delete_password(_KR_SERVICE, "__probe__")
        return keyring  # type: ignore[return-value]
    except (KeyringError, Exception) as e:  # noqa: BLE001 — capture all backend errors
        logger.warning("keyring unavailable: %s; falling back to plaintext", e)
        return None


@dataclass
class SecretRecord:
    key: str
    value: str
    method: str  # 'keyring' | 'plaintext'


class SecretStore:
    """Get/set secrets with automatic keyring/plaintext routing.

    Notes:
        * Plaintext fallback is intentional: users on headless Linux / CI
          environments need a path that works without an interactive key store.
        * `secret_store` table records which method was used so callers can
          warn on plaintext storage.
    """

    def __init__(self, db: Database, *, force_plaintext: bool = False) -> None:
        self._db = db
        self._keyring = None if force_plaintext else _try_load_keyring()

    @property
    def using_keyring(self) -> bool:
        return self._keyring is not None

    def get(self, key: str) -> str | None:
        method, value = self._read_record(key)
        if method is None:
            return None
        if method == "keyring":
            # B3.3 / M1 fix — gracefully degrade when the secret was originally
            # stored via keyring but the current environment doesn't have one
            # (e.g. user moved the DB across machines, or fell from a TTY env
            # to headless CI). Return None + clear log so callers can prompt
            # the user to re-run `deeptrade config set`.
            if self._keyring is None:
                logger.error(
                    "secret %r is stored in keyring but no keyring is available "
                    "in this environment; re-run `deeptrade config set` to "
                    "migrate to plaintext or fix keyring access.",
                    key,
                )
                return None
            return self._keyring.get_password(_KR_SERVICE, key)
        # plaintext: value column holds the raw bytes
        return value

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            self._keyring.set_password(_KR_SERVICE, key, value)
            self._upsert_record(key, encrypted_value=b"", method="keyring")
        else:
            logger.warning(
                "Storing %r as plaintext in secret_store (keyring unavailable). "
                "Anyone with read access to the DuckDB file can read this secret.",
                key,
            )
            self._upsert_record(
                key,
                encrypted_value=value.encode("utf-8"),
                method="plaintext",
            )

    def delete(self, key: str) -> None:
        method, _ = self._read_record(key)
        if method == "keyring" and self._keyring is not None:
            try:
                self._keyring.delete_password(_KR_SERVICE, key)
            except Exception:  # noqa: BLE001 — keyring may not have it
                pass
        self._db.execute("DELETE FROM secret_store WHERE key = ?", (key,))

    def list_records(self) -> list[SecretRecord]:
        rows = self._db.fetchall(
            "SELECT key, encryption_method, encrypted_value FROM secret_store ORDER BY key"
        )
        out: list[SecretRecord] = []
        for key, method, blob in rows:
            value = (blob.decode("utf-8") if blob else "") if method == "plaintext" else ""
            out.append(SecretRecord(key=key, value=value, method=method))
        return out

    # --- internal -----------------------------------------------------

    def _read_record(self, key: str) -> tuple[str | None, str | None]:
        row = self._db.fetchone(
            "SELECT encryption_method, encrypted_value FROM secret_store WHERE key = ?",
            (key,),
        )
        if row is None:
            return None, None
        method, blob = row
        return method, (blob.decode("utf-8") if blob else "")

    def _upsert_record(self, key: str, *, encrypted_value: bytes, method: str) -> None:
        # DuckDB lacks ON CONFLICT in older versions; do delete+insert in a tx
        with self._db.transaction():
            self._db.execute("DELETE FROM secret_store WHERE key = ?", (key,))
            self._db.execute(
                "INSERT INTO secret_store(key, encrypted_value, encryption_method) "
                "VALUES (?, ?, ?)",
                (key, encrypted_value, method),
            )
