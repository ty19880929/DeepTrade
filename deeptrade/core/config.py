"""Configuration management.

Layered priority (DESIGN §7.2):
    env var > secret_store (for secrets) > app_config (for non-secrets) > Pydantic default

v0.6 — LLM 配置抬升为框架层基础能力（DESIGN §0.7 / §10）：
    * `llm.providers`            — JSON dict 形态，多 provider 同时存在；app_config
    * `llm.<name>.api_key`       — 每 provider 一把，secret_store；前缀匹配路由
    * `llm.audit_full_payload`   — 全局审计 verbosity；app_config

v0.7 — stage 概念彻底归插件：删除 ``DS_STAGES`` / ``DeepSeekProfileSet`` /
``PROFILES_DEFAULT`` / ``ConfigService.get_profile()``；preset 名仍框架级，
但"preset → 各 stage tuning"由插件自己维护。配置键 ``deepseek.profile``
更名为 ``app.profile``（vendor-agnostic）；旧键自动迁移见
``config_migrations.migrate_legacy_deepseek_profile_key``。环境变量
``DEEPTRADE_DEEPSEEK_PROFILE`` 在 v0.7 直接断代，启动时检测到旧 env
而新 env 未设会**报错退出**，避免静默用错配置。

Secrets are stored in the ``secret_store`` table (encrypted via keyring or
plaintext fallback) and never written to ``app_config``. The reverse is also
true: non-secrets never go into ``secret_store``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deeptrade.core.db import Database
from deeptrade.core.secrets import SecretStore
from deeptrade.plugins_api.llm import StageProfile

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

# StageProfile is imported from plugins_api.llm (v0.7 — stage 概念归插件)。
# Re-imported here so `from deeptrade.core.config import StageProfile` keeps
# working through the migration window; new code should import directly from
# ``deeptrade.plugins_api``.
_ = StageProfile  # silence ruff F401 — symbol is intentionally re-exported


class LLMProviderConfig(BaseModel):
    """One LLM provider entry — connection metadata only.

    The api_key is NOT stored here; it lives in ``secret_store`` under the
    key ``llm.<name>.api_key`` and is routed via ``is_secret_key()``.

    ``is_default`` marks the provider used by ``LLMManager.get_client()``
    when the caller does not name a provider (non-debate plugin path).
    Invariant: while ``llm_providers`` is non-empty, exactly one entry
    has ``is_default=True``. ``ConfigService.set_llm_provider`` /
    ``delete_llm_provider`` maintain this invariant; legacy configs are
    repaired by ``config_migrations``.
    """

    model_config = ConfigDict(extra="forbid")
    base_url: str
    model: str
    timeout: int = Field(default=180, ge=10)
    is_default: bool = False


class AppConfig(BaseModel):
    """Top-level non-secret config. DESIGN §7.1.

    Defaults are designed so a freshly-installed CLI works on first
    `init`; secrets (``tushare.token``, ``llm.<name>.api_key``) are
    intentionally not part of this model — they live in SecretStore.
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
    # Tenacity stop_after_attempt for transient errors (rate limit / server /
    # transport). Default 7 keeps worst-case wait around one minute of
    # jittered exponential backoff. Each attempt re-enters the token bucket,
    # so retries never bypass rate limiting.
    tushare_max_retries: int = Field(default=7, ge=1, le=20)

    # Global preset name. v0.7 — renamed from ``deepseek.profile``; semantics
    # are vendor-agnostic. Per-stage tuning is resolved by each plugin's
    # ``profiles.py`` from this preset string.
    app_profile: Literal["fast", "balanced", "quality"] = "balanced"

    # v0.6 multi-provider LLM config
    llm_providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    # When False (default), llm_calls stores prompt_hash + a short response
    # excerpt only; full prompt/response always go to
    # ~/.deeptrade/reports/<run_id>/llm_calls.jsonl. When True (debug), DB rows
    # also keep the full payloads.
    llm_audit_full_payload: bool = False

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

    @field_validator("llm_providers", mode="before")
    @classmethod
    def _parse_llm_providers(cls, v: Any) -> Any:
        # env var path delivers a JSON string; DB path delivers an already-parsed dict
        if isinstance(v, str):
            return json.loads(v)
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
    "tushare.max_retries": "tushare_max_retries",
    "app.profile": "app_profile",
    "llm.providers": "llm_providers",
    "llm.audit_full_payload": "llm_audit_full_payload",
}

# Per-provider api_key keys are matched dynamically; only static secret keys
# enumerated here for `known_keys()` / show.
_STATIC_SECRET_KEYS: frozenset[str] = frozenset({"tushare.token"})

# Pattern for per-provider api_key routing: llm.<name>.api_key where <name>
# is non-empty and contains no dot. Any key matching this routes to
# secret_store; others fall through to app_config.
_LLM_API_KEY_RE = re.compile(r"^llm\.([^.]+)\.api_key$")


def is_secret_key(key: str) -> bool:
    """True iff ``key`` should route to secret_store instead of app_config.

    Static secrets: ``tushare.token``.
    Dynamic secrets: ``llm.<name>.api_key`` for any provider name.
    """
    if key in _STATIC_SECRET_KEYS:
        return True
    return bool(_LLM_API_KEY_RE.match(key))


def llm_api_key_name(key: str) -> str | None:
    """If ``key`` is an ``llm.<name>.api_key``, return ``<name>``; else None."""
    m = _LLM_API_KEY_RE.match(key)
    return m.group(1) if m else None


def env_var_for(key: str) -> str:
    """Map dotted key → ``DEEPTRADE_<UPPER_SNAKE>`` env var name.

    Hyphens are normalized to underscores so that hyphen-containing provider
    names (e.g. ``llm.qwen-plus.api_key``) yield a POSIX-valid identifier
    (``DEEPTRADE_LLM_QWEN_PLUS_API_KEY``). Without this, the env var
    ``DEEPTRADE_LLM_QWEN-PLUS_API_KEY`` cannot be set via ``export`` on
    bash / sh — variable names there must match ``[A-Za-z_][A-Za-z0-9_]*``.

    Collision risk: provider names ``qwen-plus`` and ``qwen_plus`` would map
    to the same env var. :meth:`ConfigService.set_llm_provider` refuses such
    a registration so users discover the conflict at write time rather than
    at first env-var read.
    """
    return "DEEPTRADE_" + key.upper().replace(".", "_").replace("-", "_")


def known_keys() -> list[str]:
    """Static known keys. Per-provider ``llm.<name>.api_key`` entries are
    dynamic and not enumerated here; CLI `set-llm` handles those.
    """
    return sorted(list(_DOT_TO_FIELD.keys()) + list(_STATIC_SECRET_KEYS))


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

    # --- introspection -------------------------------------------------

    def count_plaintext_secrets(self) -> int:
        """Return the number of secrets currently stored as plaintext in
        ``secret_store``. Used by ``deeptrade config show`` (T13) to flag
        the unencrypted-keys situation that arises on hosts without an
        accessible OS keyring."""
        return sum(1 for r in self._secrets.list_records() if r.method == "plaintext")

    # --- read ----------------------------------------------------------

    def get(self, key: str) -> Any:
        """Return the resolved value for *key* (None if absent and no default)."""
        env = os.environ.get(env_var_for(key))
        if env is not None:
            # llm.providers env override is a JSON string; decode for callers
            if key == "llm.providers":
                return json.loads(env)
            return env

        if is_secret_key(key):
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
        if is_secret_key(key):
            return "secret_store" if self._secrets.get(key) is not None else "default"
        row = self._db.fetchone("SELECT value_json FROM app_config WHERE key = ?", (key,))
        return "app_config" if row is not None else "default"

    def get_app_config(self) -> AppConfig:
        """Materialize a fully-resolved AppConfig (env > db > default)."""
        # v0.7 — env var DEEPTRADE_DEEPSEEK_PROFILE was renamed to
        # DEEPTRADE_APP_PROFILE. Hard-stop on the legacy name to prevent
        # silently using the (Pydantic) default when the user thinks they've
        # configured something. DB rows are migrated automatically (see
        # config_migrations.migrate_legacy_deepseek_profile_key); env vars
        # cannot be auto-migrated, so we surface the error explicitly.
        if "DEEPTRADE_DEEPSEEK_PROFILE" in os.environ and "DEEPTRADE_APP_PROFILE" not in os.environ:
            raise RuntimeError(
                "DEEPTRADE_DEEPSEEK_PROFILE was renamed to DEEPTRADE_APP_PROFILE in "
                "v0.7 and is no longer recognized. Update your environment to set "
                "DEEPTRADE_APP_PROFILE (or unset DEEPTRADE_DEEPSEEK_PROFILE)."
            )

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

    # v0.7 — get_profile() removed. Stage 概念已归插件；调用方读取
    # ``get_app_config().app_profile`` 拿 preset 字符串后，由插件本地
    # 的 ``profiles.py`` 解析为 ``StageProfile``。

    # --- write ---------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Route to secret_store or app_config based on key namespace."""
        if is_secret_key(key):
            self._secrets.set(key, str(value))
            return

        if key not in _DOT_TO_FIELD:
            raise ValueError(f"unknown config key: {key!r}; see `deeptrade config show`")

        # Validate by constructing a partial AppConfig; capture normalized JSON
        # representation so nested Pydantic models / time / dicts all serialize
        # cleanly without ad-hoc isinstance branches.
        field = _DOT_TO_FIELD[key]
        validated = AppConfig(**{field: value})
        normalized = validated.model_dump(mode="json").get(field)
        payload = json.dumps(normalized)

        with self._db.transaction():
            self._db.execute("DELETE FROM app_config WHERE key = ?", (key,))
            self._db.execute(
                "INSERT INTO app_config(key, value_json, is_secret) VALUES (?, ?, ?)",
                (key, payload, False),
            )

    def delete(self, key: str) -> None:
        if is_secret_key(key):
            self._secrets.delete(key)
        else:
            self._db.execute("DELETE FROM app_config WHERE key = ?", (key,))

    # --- listing -------------------------------------------------------

    def list_all(self) -> list[tuple[str, Any, str]]:
        """Return [(key, value (masked for secrets), source)] for `config show`.

        Includes all static known keys and one row per configured LLM provider's
        api_key (so the user sees every secret slot at a glance).
        """
        out: list[tuple[str, Any, str]] = []
        for key in known_keys():
            value = self.get(key)
            source = self.source_of(key)
            if is_secret_key(key) and value:
                value = f"********{str(value)[-4:]}"
            out.append((key, value, source))
        # Per-provider api_key rows
        cfg = self.get_app_config()
        for provider_name in sorted(cfg.llm_providers.keys()):
            secret_key = f"llm.{provider_name}.api_key"
            value = self._secrets.get(secret_key)
            source = "secret_store" if value else "default"
            display: Any = f"********{str(value)[-4:]}" if value else None
            out.append((secret_key, display, source))
        return out

    # --- LLM provider CRUD --------------------------------------------

    def set_llm_provider(
        self,
        name: str,
        *,
        base_url: str,
        model: str,
        timeout: int = 180,
        api_key: str | None = None,
        is_default: bool | None = None,
    ) -> None:
        """Insert or update a provider entry. If ``api_key`` is given, also
        persist it to secret_store under ``llm.<name>.api_key``.

        Default-provider invariant (one entry has ``is_default=True`` while
        ``llm.providers`` is non-empty) is maintained here:

          * Adding the first provider auto-marks it default regardless of
            the ``is_default`` argument.
          * ``is_default=True`` on a non-first add or update promotes this
            provider and clears the flag on every other entry.
          * ``is_default=None`` on update preserves the existing flag (no
            silent demotion); on a non-first add it defaults to False.
          * ``is_default=False`` is honored only when another provider
            already holds the default flag — never used to leave the dict
            with zero defaults.
        """
        if not name or "." in name:
            raise ValueError(
                f"invalid provider name: {name!r}; must be non-empty and contain no '.'"
            )
        current_raw = self.get("llm.providers")
        current: dict[str, Any] = dict(current_raw) if isinstance(current_raw, dict) else {}

        # M6 — refuse a name that collides with an existing provider after
        # ``env_var_for`` normalization (``-`` → ``_``). Without this, two
        # providers ``qwen-plus`` and ``qwen_plus`` would share the env var
        # ``DEEPTRADE_LLM_QWEN_PLUS_API_KEY`` and silently shadow each other
        # on read. Surface the conflict at registration time.
        normalized = name.replace("-", "_")
        for existing_name in current:
            if existing_name == name:
                continue
            if existing_name.replace("-", "_") == normalized:
                raise ValueError(
                    f"provider name {name!r} collides with {existing_name!r} after "
                    f"env-var normalization (both map to "
                    f"{env_var_for(f'llm.{name}.api_key')!r}); rename one to avoid "
                    f"silent env-var shadowing"
                )

        is_first = len(current) == 0
        existing = dict(current.get(name) or {})
        prior_default = bool(existing.get("is_default", False))

        # Resolve the new is_default flag for this provider.
        if is_first:
            new_default = True
        elif is_default is True:
            new_default = True
        elif is_default is None:
            new_default = prior_default
        else:  # is_default is False
            # Only honor an explicit demotion if some other provider is
            # already default; otherwise we'd leave the dict with no
            # default at all. Preserve prior_default in that case.
            other_has_default = any(
                k != name and bool((v or {}).get("is_default")) for k, v in current.items()
            )
            new_default = prior_default if not other_has_default else False

        # If this provider is becoming default, demote every other entry so
        # the invariant "at most one default" holds.
        if new_default:
            for other_name, other_cfg in current.items():
                if (
                    other_name != name
                    and isinstance(other_cfg, dict)
                    and other_cfg.get("is_default")
                ):
                    current[other_name] = {**other_cfg, "is_default": False}

        current[name] = {
            "base_url": base_url,
            "model": model,
            "timeout": timeout,
            "is_default": new_default,
        }
        self.set("llm.providers", current)
        if api_key is not None:
            self.set(f"llm.{name}.api_key", api_key)

    def delete_llm_provider(self, name: str) -> None:
        """Remove a provider entry plus its api_key. Idempotent on missing name.

        If the removed provider held ``is_default=True`` and ≥1 entry
        survives, the first remaining entry (insertion order) is auto-
        promoted to default to maintain the invariant.
        """
        current_raw = self.get("llm.providers")
        current: dict[str, Any] = dict(current_raw) if isinstance(current_raw, dict) else {}
        removed = current.pop(name, None)
        removed_was_default = bool(isinstance(removed, dict) and removed.get("is_default"))

        if removed_was_default and current:
            first_key = next(iter(current.keys()))
            first_cfg = current[first_key]
            if isinstance(first_cfg, dict):
                current[first_key] = {**first_cfg, "is_default": True}

        if current:
            self.set("llm.providers", current)
        else:
            self.delete("llm.providers")
        self.delete(f"llm.{name}.api_key")

    def get_default_llm_provider(self) -> str | None:
        """Return the name of the provider with ``is_default=True``, or None
        if no providers are configured.

        Used by :class:`LLMManager.get_client` to resolve ``name=None`` calls
        from non-debate plugins. Not exposed as a CLI; the CLI sets the
        default via ``set-default-llm`` which routes through
        :meth:`set_llm_provider`.
        """
        cfg = self.get_app_config()
        for provider_name, provider in cfg.llm_providers.items():
            if provider.is_default:
                return provider_name
        return None
