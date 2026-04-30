"""Configuration management.

Layered priority (DESIGN §7.2):
    env var > secret_store (for secrets) > app_config (for non-secrets) > Pydantic default

Secrets are stored in the ``secret_store`` table (encrypted via keyring or
plaintext fallback) and never written to ``app_config``. The reverse is also
true: non-secrets never go into ``secret_store``.

DeepSeek profile config: §10.1 + S1 fix (stage-level max_output_tokens
inside each profile so R1 isn't capped at 8k tokens).
"""

from __future__ import annotations

import json
import os
from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deeptrade.core.db import Database
from deeptrade.core.secrets import SecretStore

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

# Stage names valid in DeepSeek profiles (§10.1)
DS_STAGES = ("strong_target_analysis", "continuation_prediction", "final_ranking")


class StageProfile(BaseModel):
    """Per-stage LLM tuning. F5 fix: max_output_tokens is per-stage."""

    model_config = ConfigDict(extra="forbid")
    thinking: bool
    reasoning_effort: Literal["low", "medium", "high"]
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(ge=1024, le=384_000)


class DeepSeekProfileSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strong_target_analysis: StageProfile
    continuation_prediction: StageProfile
    final_ranking: StageProfile


# Built-in profile presets per DESIGN §10.1 (incorporates F3 fast=all-off, F5 stage-tokens)
PROFILES_DEFAULT: dict[str, DeepSeekProfileSet] = {
    "fast": DeepSeekProfileSet(
        strong_target_analysis=StageProfile(
            thinking=False, reasoning_effort="medium", temperature=0.1, max_output_tokens=32768
        ),
        continuation_prediction=StageProfile(
            thinking=False, reasoning_effort="medium", temperature=0.2, max_output_tokens=32768
        ),
        final_ranking=StageProfile(
            thinking=False, reasoning_effort="medium", temperature=0.0, max_output_tokens=8192
        ),
    ),
    "balanced": DeepSeekProfileSet(
        strong_target_analysis=StageProfile(
            thinking=False, reasoning_effort="medium", temperature=0.1, max_output_tokens=32768
        ),
        continuation_prediction=StageProfile(
            thinking=True, reasoning_effort="high", temperature=0.2, max_output_tokens=32768
        ),
        final_ranking=StageProfile(
            thinking=True, reasoning_effort="high", temperature=0.0, max_output_tokens=8192
        ),
    ),
    "quality": DeepSeekProfileSet(
        strong_target_analysis=StageProfile(
            thinking=True, reasoning_effort="high", temperature=0.2, max_output_tokens=32768
        ),
        continuation_prediction=StageProfile(
            thinking=True, reasoning_effort="high", temperature=0.2, max_output_tokens=32768
        ),
        final_ranking=StageProfile(
            thinking=True, reasoning_effort="high", temperature=0.0, max_output_tokens=8192
        ),
    ),
}


class AppConfig(BaseModel):
    """Top-level non-secret config. DESIGN §7.1.

    Defaults are designed so a freshly-installed CLI works on first
    `init`; secrets (tushare.token, deepseek.api_key) are intentionally
    not part of this model — they live in SecretStore.
    """

    model_config = ConfigDict(extra="forbid")

    # app.*
    app_timezone: str = "Asia/Shanghai"
    app_locale: str = "zh_CN"
    app_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # S2 fix: data close threshold is configurable (used by §12.2)
    app_close_after: time = time(18, 0)

    # tushare.*  (token lives in secret_store)
    tushare_rps: float = Field(default=6.0, gt=0)
    tushare_timeout: int = Field(default=30, ge=1)

    # deepseek.*  (api_key lives in secret_store)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_profile: Literal["fast", "balanced", "quality"] = "balanced"
    deepseek_timeout: int = Field(default=180, ge=10)

    # M4: LLM audit verbosity. When False (default), llm_calls stores prompt_hash
    # + a short response excerpt only; full prompt/response always go to
    # ~/.deeptrade/reports/<run_id>/llm_calls.jsonl. When True (debug), DB rows
    # also keep the full payloads.
    deepseek_audit_full_payload: bool = False

    @field_validator("app_close_after", mode="before")
    @classmethod
    def _parse_close_after(cls, v: Any) -> Any:
        if isinstance(v, str):
            # Accept "HH:MM" or "HH:MM:SS"
            parts = v.split(":")
            if len(parts) == 2:
                return time(int(parts[0]), int(parts[1]))
            if len(parts) == 3:
                return time(int(parts[0]), int(parts[1]), int(parts[2]))
        return v


# ---------------------------------------------------------------------------
# Key namespace mapping
# ---------------------------------------------------------------------------

# Translates dotted keys (user-facing) ↔ AppConfig field names
_DOT_TO_FIELD: dict[str, str] = {
    "app.timezone": "app_timezone",
    "app.locale": "app_locale",
    "app.log_level": "app_log_level",
    "app.close_after": "app_close_after",
    "tushare.rps": "tushare_rps",
    "tushare.timeout": "tushare_timeout",
    "deepseek.base_url": "deepseek_base_url",
    "deepseek.model": "deepseek_model",
    "deepseek.profile": "deepseek_profile",
    "deepseek.timeout": "deepseek_timeout",
    "deepseek.audit_full_payload": "deepseek_audit_full_payload",
}

# Keys that route to SecretStore instead of app_config
SECRET_KEYS = frozenset({"tushare.token", "deepseek.api_key"})


def env_var_for(key: str) -> str:
    """Map dotted key → DEEPTRADE_<UPPER_SNAKE> env var name."""
    return "DEEPTRADE_" + key.upper().replace(".", "_")


def known_keys() -> list[str]:
    return sorted(list(_DOT_TO_FIELD.keys()) + list(SECRET_KEYS))


# ---------------------------------------------------------------------------
# ConfigService — read/write with layered priority
# ---------------------------------------------------------------------------


class ConfigService:
    """Read & write config with layered priority + automatic routing.

    Priority for non-secrets:  env var > app_config table > Pydantic default
    Priority for secrets:      env var > secret_store
    """

    def __init__(self, db: Database, secret_store: SecretStore | None = None) -> None:
        self._db = db
        self._secrets = secret_store if secret_store is not None else SecretStore(db)

    # --- read ----------------------------------------------------------

    def get(self, key: str) -> Any:
        """Return the resolved value for *key* (None if absent and no default)."""
        env = os.environ.get(env_var_for(key))
        if env is not None:
            return env

        if key in SECRET_KEYS:
            return self._secrets.get(key)

        # Non-secret: check app_config, then fall back to AppConfig default
        row = self._db.fetchone("SELECT value_json FROM app_config WHERE key = ?", (key,))
        if row is not None:
            return json.loads(row[0])

        # Pydantic default
        defaults = AppConfig().model_dump(mode="json")
        field = _DOT_TO_FIELD.get(key)
        if field is None:
            return None
        return defaults.get(field)

    def source_of(self, key: str) -> Literal["env", "secret_store", "app_config", "default"]:
        if os.environ.get(env_var_for(key)) is not None:
            return "env"
        if key in SECRET_KEYS:
            return "secret_store" if self._secrets.get(key) is not None else "default"
        row = self._db.fetchone("SELECT value_json FROM app_config WHERE key = ?", (key,))
        return "app_config" if row is not None else "default"

    def get_app_config(self) -> AppConfig:
        """Materialize a fully-resolved AppConfig (env > db > default)."""
        overrides: dict[str, Any] = {}
        for dotted, field in _DOT_TO_FIELD.items():
            env = os.environ.get(env_var_for(dotted))
            if env is not None:
                overrides[field] = env
                continue
            row = self._db.fetchone("SELECT value_json FROM app_config WHERE key = ?", (dotted,))
            if row is not None:
                overrides[field] = json.loads(row[0])
        return AppConfig(**overrides)

    def get_profile(self, name: str | None = None) -> DeepSeekProfileSet:
        """Return the active DeepSeek profile (defaults to current setting)."""
        if name is None:
            name = self.get_app_config().deepseek_profile
        if name not in PROFILES_DEFAULT:
            raise ValueError(f"unknown deepseek profile: {name!r}")
        return PROFILES_DEFAULT[name]

    # --- write ---------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Route to secret_store or app_config based on key namespace."""
        if key in SECRET_KEYS:
            self._secrets.set(key, str(value))
            return

        if key not in _DOT_TO_FIELD:
            raise ValueError(f"unknown config key: {key!r}; see `deeptrade config show`")

        # Validate by constructing a partial AppConfig
        field = _DOT_TO_FIELD[key]
        AppConfig(**{field: value})  # raises if invalid

        # Persist as JSON for type fidelity (e.g. time → "HH:MM:SS")
        if isinstance(value, time):
            payload = json.dumps(value.strftime("%H:%M:%S"))
        else:
            payload = json.dumps(value)

        with self._db.transaction():
            self._db.execute("DELETE FROM app_config WHERE key = ?", (key,))
            self._db.execute(
                "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
                (key, payload, False),
            )

    def delete(self, key: str) -> None:
        if key in SECRET_KEYS:
            self._secrets.delete(key)
        else:
            self._db.execute("DELETE FROM app_config WHERE key = ?", (key,))

    # --- listing -------------------------------------------------------

    def list_all(self) -> list[tuple[str, Any, str]]:
        """Return [(key, value (masked for secrets), source)] for `config show`."""
        out: list[tuple[str, Any, str]] = []
        for key in known_keys():
            value = self.get(key)
            source = self.source_of(key)
            if key in SECRET_KEYS and value:
                # Mask: show only last 4 chars
                value = f"********{str(value)[-4:]}"
            out.append((key, value, source))
        return out
