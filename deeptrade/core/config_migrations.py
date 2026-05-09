"""Idempotent data migrations for the framework's app_config / secret_store.

Schema-shape migrations live as SQL files in ``migrations/core/``. This module
holds *data-shape* migrations — ones that rewrite existing rows into a new
key namespace without touching DDL. Each function is idempotent (short-
circuits when its target state is already present), so re-running on a fresh
or already-migrated DB is a no-op.

v0.6 — deepseek.* → llm.providers / llm.<name>.api_key (DESIGN §0.7 / §10.5).
v0.7 — deepseek.profile → app.profile (DESIGN §10.1 update).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.db import Database


_LEGACY_KEYS = (
    "deepseek.base_url",
    "deepseek.model",
    "deepseek.timeout",
    "deepseek.audit_full_payload",
)


def migrate_legacy_deepseek_keys(db: Database) -> bool:
    """Migrate legacy ``deepseek.*`` config keys to the v0.6 ``llm.*`` schema.

    Idempotent: if ``llm.providers`` is already present, skip everything.
    Otherwise:

      1. Read any of ``deepseek.base_url`` / ``deepseek.model`` /
         ``deepseek.timeout`` from app_config; if all three are absent, treat
         this as a fresh DB (no legacy data) and return False.
      2. Build ``llm.providers["deepseek"] = {base_url, model, timeout}`` using
         AppConfig defaults for any field not present.
      3. If ``deepseek.audit_full_payload`` is present, copy to
         ``llm.audit_full_payload``.
      4. Rename the secret_store row ``deepseek.api_key`` →
         ``llm.deepseek.api_key`` (preserving encrypted_value /
         encryption_method).
      5. Delete the four legacy app_config rows.

    ``deepseek.profile`` migration is handled separately in v0.7 by
    :func:`migrate_legacy_deepseek_profile_key`.

    Returns True iff a migration was performed.
    """
    # Idempotency: if llm.providers exists, we've already migrated (or the user
    # has set up v0.6 fresh). Don't touch anything.
    row = db.fetchone("SELECT 1 FROM app_config WHERE key = 'llm.providers'")
    if row is not None:
        return False

    legacy_rows = db.fetchall(
        "SELECT key, value_json FROM app_config WHERE key IN (?, ?, ?, ?)",
        _LEGACY_KEYS,
    )
    if not legacy_rows:
        return False

    legacy: dict[str, object] = {k: json.loads(v) for k, v in legacy_rows}

    # Defaults match AppConfig pre-v0.6 defaults so a partially-set DB
    # (e.g. user only ever set deepseek.api_key) still produces a usable
    # llm.providers["deepseek"] entry.
    base_url = legacy.get("deepseek.base_url", "https://api.deepseek.com")
    model = legacy.get("deepseek.model", "deepseek-v4-pro")
    timeout = legacy.get("deepseek.timeout", 180)
    audit_full = legacy.get("deepseek.audit_full_payload")

    providers = {
        "deepseek": {
            "base_url": base_url,
            "model": model,
            "timeout": timeout,
            "is_default": True,
        }
    }

    with db.transaction():
        db.execute(
            "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
            ("llm.providers", json.dumps(providers), False),
        )
        if audit_full is not None:
            db.execute(
                "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
                ("llm.audit_full_payload", json.dumps(audit_full), False),
            )
        # Rename secret. UPDATE-only avoids re-encrypting; if the destination
        # row already exists (extremely unlikely on a non-migrated DB), prefer
        # the legacy value as canonical and overwrite.
        existing_dest = db.fetchone(
            "SELECT 1 FROM secret_store WHERE key = 'llm.deepseek.api_key'"
        )
        if existing_dest is not None:
            db.execute("DELETE FROM secret_store WHERE key = 'llm.deepseek.api_key'")
        db.execute(
            "UPDATE secret_store SET key = 'llm.deepseek.api_key' WHERE key = 'deepseek.api_key'"
        )
        db.execute(
            "DELETE FROM app_config WHERE key IN (?, ?, ?, ?)",
            _LEGACY_KEYS,
        )

    return True


def migrate_llm_default_provider(db: Database) -> bool:
    """Backfill ``is_default`` on existing ``llm.providers`` entries.

    Until v0.8, ``LLMProviderConfig`` had no ``is_default`` field and
    plugins picked a provider via hardcoded preference. v0.8 promoted
    "default provider" to a framework-level concept; existing rows
    therefore have no ``is_default`` flag. This migration enforces the
    invariant "while llm.providers is non-empty, exactly one entry has
    is_default=True" by promoting the first entry (insertion order)
    when none are flagged.

    Idempotent: returns False on a fresh DB, an empty providers dict, or
    a dict that already has ≥1 default.

    Returns True iff a row was rewritten.
    """
    row = db.fetchone("SELECT value_json FROM app_config WHERE key = 'llm.providers'")
    if row is None:
        return False
    providers = json.loads(row[0])
    if not isinstance(providers, dict) or not providers:
        return False

    has_default = any(
        isinstance(v, dict) and bool(v.get("is_default")) for v in providers.values()
    )
    if has_default:
        return False

    first_key = next(iter(providers.keys()))
    first_cfg = providers[first_key]
    if not isinstance(first_cfg, dict):
        return False
    providers[first_key] = {**first_cfg, "is_default": True}

    with db.transaction():
        db.execute(
            "UPDATE app_config SET value_json = ? WHERE key = 'llm.providers'",
            (json.dumps(providers),),
        )
    return True


def migrate_legacy_deepseek_profile_key(db: Database) -> bool:
    """Migrate ``deepseek.profile`` → ``app.profile`` (v0.7).

    v0.7 promoted the global stage-profile preset name from a vendor-prefixed
    key (``deepseek.profile``) to a vendor-agnostic one (``app.profile``).
    Stage 调参档已彻底归插件，preset 名仍框架级，但键名重命名以反映其与
    DeepSeek 无关。

    Idempotent: skip when ``app.profile`` already exists; otherwise copy the
    value and delete the legacy row.

    Returns True iff a migration was performed.
    """
    new_row = db.fetchone("SELECT 1 FROM app_config WHERE key = 'app.profile'")
    if new_row is not None:
        return False
    legacy = db.fetchone("SELECT value_json FROM app_config WHERE key = 'deepseek.profile'")
    if legacy is None:
        return False
    with db.transaction():
        db.execute(
            "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
            ("app.profile", legacy[0], False),
        )
        db.execute("DELETE FROM app_config WHERE key = 'deepseek.profile'")
    return True
