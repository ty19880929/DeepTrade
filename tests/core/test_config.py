"""V0.2 DoD — ConfigService priority + secret routing + profile loading.

v0.6 — `SECRET_KEYS` was retired in favor of `is_secret_key()` (prefix
matching for `llm.<name>.api_key`). Tests updated accordingly.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from deeptrade.core.config import (
    AppConfig,
    ConfigService,
    env_var_for,
    is_secret_key,
)
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.secrets import SecretStore


@pytest.fixture
def svc(tmp_path: Path) -> ConfigService:
    db = Database(tmp_path / "test.duckdb")
    apply_core_migrations(db)
    return ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))


# --- DoD 1: secrets are masked in list_all ---------------------------------


def test_config_show_masks_secrets(svc: ConfigService) -> None:
    svc.set("tushare.token", "abcdef1234567890")
    rendered = dict((k, v) for k, v, _src in svc.list_all())
    assert rendered["tushare.token"].startswith("********")
    assert "abcdef" not in rendered["tushare.token"]
    assert rendered["tushare.token"].endswith("7890")  # last-4 visible


# --- DoD 2: env var overrides db and default --------------------------------


def test_env_overrides_db_and_default(svc: ConfigService, monkeypatch: pytest.MonkeyPatch) -> None:
    # default
    assert svc.get("app.profile") == "balanced"
    # db override
    svc.set("app.profile", "quality")
    assert svc.get("app.profile") == "quality"
    # env var beats db
    monkeypatch.setenv(env_var_for("app.profile"), "fast")
    assert svc.get("app.profile") == "fast"
    assert svc.source_of("app.profile") == "env"


def test_legacy_deepseek_profile_env_raises(
    svc: ConfigService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.7: legacy DEEPTRADE_DEEPSEEK_PROFILE must hard-stop on materialize."""
    monkeypatch.setenv("DEEPTRADE_DEEPSEEK_PROFILE", "fast")
    monkeypatch.delenv("DEEPTRADE_APP_PROFILE", raising=False)
    with pytest.raises(RuntimeError, match="DEEPTRADE_DEEPSEEK_PROFILE"):
        svc.get_app_config()


def test_legacy_deepseek_profile_env_silent_when_new_env_set(
    svc: ConfigService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user sets BOTH old and new env, defer to new — no error."""
    monkeypatch.setenv("DEEPTRADE_DEEPSEEK_PROFILE", "fast")
    monkeypatch.setenv("DEEPTRADE_APP_PROFILE", "quality")
    cfg = svc.get_app_config()
    assert cfg.app_profile == "quality"


def test_env_var_naming_convention() -> None:
    assert env_var_for("tushare.token") == "DEEPTRADE_TUSHARE_TOKEN"
    assert env_var_for("llm.deepseek.api_key") == "DEEPTRADE_LLM_DEEPSEEK_API_KEY"
    assert env_var_for("app.close_after") == "DEEPTRADE_APP_CLOSE_AFTER"


def test_env_var_for_normalizes_hyphenated_provider_names() -> None:
    """v0.6 M6 — hyphens in provider names must collapse to underscores so
    the env var is a POSIX-valid identifier (``[A-Za-z_][A-Za-z0-9_]*``).
    Without this, ``DEEPTRADE_LLM_QWEN-PLUS_API_KEY`` cannot be set via
    bash/sh ``export``."""
    assert env_var_for("llm.qwen-plus.api_key") == "DEEPTRADE_LLM_QWEN_PLUS_API_KEY"
    assert env_var_for("llm.qwen-max-2024.api_key") == "DEEPTRADE_LLM_QWEN_MAX_2024_API_KEY"


def test_set_llm_provider_refuses_hyphen_normalization_collision(
    svc: ConfigService,
) -> None:
    """v0.6 M6 — registering two providers whose names normalize to the
    same env var (``qwen-plus`` vs ``qwen_plus``) must fail at write time
    rather than silently shadowing each other on read."""
    svc.set_llm_provider(
        "qwen-plus",
        base_url="https://example.com",
        model="qwen-plus",
        api_key="sk-1",
    )
    with pytest.raises(ValueError, match="collides with"):
        svc.set_llm_provider(
            "qwen_plus",
            base_url="https://example.com",
            model="qwen-plus",
            api_key="sk-2",
        )


def test_set_llm_provider_allows_updating_same_name(svc: ConfigService) -> None:
    """Re-saving an existing provider must NOT trip the collision check —
    a name only collides with *other* entries."""
    svc.set_llm_provider(
        "qwen-plus",
        base_url="https://a",
        model="qwen-plus",
        api_key="sk-1",
    )
    # Same name, new model — should succeed.
    svc.set_llm_provider(
        "qwen-plus",
        base_url="https://a",
        model="qwen-turbo",
        api_key="sk-1",
    )
    cfg = svc.get_app_config()
    assert cfg.llm_providers["qwen-plus"].model == "qwen-turbo"


# --- DoD 3 & 4: secret vs non-secret routing -------------------------------


def test_set_secret_routes_to_secret_store_only(svc: ConfigService) -> None:
    svc.set("llm.deepseek.api_key", "sk-abc")
    # app_config table must NOT contain the secret
    rows = svc._db.fetchall(  # type: ignore[attr-defined]
        "SELECT key FROM app_config WHERE key='llm.deepseek.api_key'"
    )
    assert rows == []
    # secret_store DOES
    rows = svc._db.fetchall(  # type: ignore[attr-defined]
        "SELECT key FROM secret_store WHERE key='llm.deepseek.api_key'"
    )
    assert rows and rows[0][0] == "llm.deepseek.api_key"
    assert svc.get("llm.deepseek.api_key") == "sk-abc"


def test_set_non_secret_routes_to_app_config_only(svc: ConfigService) -> None:
    svc.set("tushare.rps", 10.0)
    rows = svc._db.fetchall("SELECT value_json FROM app_config WHERE key='tushare.rps'")  # type: ignore[attr-defined]
    assert rows and rows[0][0] == "10.0"
    # secret_store stays empty
    rows = svc._db.fetchall("SELECT key FROM secret_store WHERE key='tushare.rps'")  # type: ignore[attr-defined]
    assert rows == []


def test_is_secret_key_routing() -> None:
    """v0.6: is_secret_key matches tushare.token + llm.<name>.api_key prefix."""
    assert is_secret_key("tushare.token") is True
    assert is_secret_key("llm.deepseek.api_key") is True
    assert is_secret_key("llm.qwen-plus.api_key") is True
    assert is_secret_key("llm.providers") is False
    assert is_secret_key("llm.audit_full_payload") is False
    assert is_secret_key("tushare.rps") is False
    # llm.<name>.something_else is NOT a secret
    assert is_secret_key("llm.deepseek.base_url") is False
    # extra dots inside name are NOT allowed
    assert is_secret_key("llm.foo.bar.api_key") is False


# --- DoD 5: invalid value rejection ----------------------------------------


def test_set_unknown_key_raises(svc: ConfigService) -> None:
    with pytest.raises(ValueError, match="unknown config key"):
        svc.set("unknown.key", "x")


def test_set_invalid_profile_value_raises(svc: ConfigService) -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError varies
        svc.set("app.profile", "ultra")


# --- DoD 6: close_after default = 18:00 (S2 fix) --------------------------


def test_close_after_default_is_18_00(svc: ConfigService) -> None:
    cfg = svc.get_app_config()
    assert cfg.app_close_after == time(18, 0)


# --- DoD 7: close_after can be overridden ---------------------------------


def test_close_after_can_be_overridden(svc: ConfigService) -> None:
    svc.set("app.close_after", "17:30")
    cfg = svc.get_app_config()
    assert cfg.app_close_after == time(17, 30)


# --- profile preset key ----------------------------------------------------
# v0.7: stage 调参档归插件，框架只保留 preset 名作为字符串配置；具体每档的
# stage tuning 由各插件 profiles.py 维护，对应单测见各插件包内。


def test_get_app_config_picks_up_env_var(
    svc: ConfigService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var_for("tushare.rps"), "12.5")
    cfg = svc.get_app_config()
    assert cfg.tushare_rps == 12.5


def test_app_config_close_after_parses_string() -> None:
    """Direct AppConfig validator handles HH:MM string from JSON."""
    cfg = AppConfig.model_validate({"app_close_after": "17:00"})
    assert cfg.app_close_after == time(17, 0)


def test_delete_secret(svc: ConfigService) -> None:
    svc.set("tushare.token", "x")
    assert svc.get("tushare.token") == "x"
    svc.delete("tushare.token")
    assert svc.get("tushare.token") is None


# --- v0.6 LLM provider CRUD -----------------------------------------------


def test_set_llm_provider_persists_config_and_api_key(svc: ConfigService) -> None:
    svc.set_llm_provider(
        "deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        timeout=120,
        api_key="sk-test",
    )
    cfg = svc.get_app_config()
    assert "deepseek" in cfg.llm_providers
    assert cfg.llm_providers["deepseek"].base_url == "https://api.deepseek.com"
    assert cfg.llm_providers["deepseek"].timeout == 120
    assert svc.get("llm.deepseek.api_key") == "sk-test"


def test_set_llm_provider_rejects_dot_in_name(svc: ConfigService) -> None:
    with pytest.raises(ValueError, match="invalid provider name"):
        svc.set_llm_provider("foo.bar", base_url="x", model="y")


def test_delete_llm_provider_clears_config_and_api_key(svc: ConfigService) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2")
    svc.delete_llm_provider("deepseek")
    cfg = svc.get_app_config()
    assert "deepseek" not in cfg.llm_providers
    assert "kimi" in cfg.llm_providers
    assert svc.get("llm.deepseek.api_key") is None
    assert svc.get("llm.kimi.api_key") == "sk-2"


def test_list_all_includes_per_provider_api_key_rows(svc: ConfigService) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-abcd1234")
    rendered = {k: (v, src) for k, v, src in svc.list_all()}
    assert "llm.deepseek.api_key" in rendered
    val, src = rendered["llm.deepseek.api_key"]
    assert val.startswith("********") and val.endswith("1234")
    assert src == "secret_store"


# --- v0.8 default LLM provider invariant ----------------------------------


def test_first_provider_added_is_auto_default(svc: ConfigService) -> None:
    """Adding into an empty providers dict auto-marks the entry default
    regardless of the is_default argument — invariant: ≥1 provider ⇒
    exactly one default."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    cfg = svc.get_app_config()
    assert cfg.llm_providers["deepseek"].is_default is True
    assert svc.get_default_llm_provider() == "deepseek"


def test_first_provider_auto_default_overrides_explicit_false(svc: ConfigService) -> None:
    """Even with is_default=False, the first provider must be promoted to
    default — otherwise the invariant breaks immediately."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1", is_default=False)
    assert svc.get_default_llm_provider() == "deepseek"


def test_subsequent_provider_defaults_to_non_default(svc: ConfigService) -> None:
    """Adding a second provider without is_default keeps the existing
    default in place."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2")
    cfg = svc.get_app_config()
    assert cfg.llm_providers["deepseek"].is_default is True
    assert cfg.llm_providers["kimi"].is_default is False
    assert svc.get_default_llm_provider() == "deepseek"


def test_explicit_default_demotes_existing(svc: ConfigService) -> None:
    """is_default=True on a non-first add must clear the flag on every
    other entry."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2", is_default=True)
    cfg = svc.get_app_config()
    assert cfg.llm_providers["deepseek"].is_default is False
    assert cfg.llm_providers["kimi"].is_default is True
    assert svc.get_default_llm_provider() == "kimi"


def test_update_preserves_existing_default_flag(svc: ConfigService) -> None:
    """Editing a provider with is_default=None must not change its flag —
    common path for the CLI 'edit existing' flow."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2", is_default=True)
    # Edit deepseek (currently non-default); flag must stay False.
    svc.set_llm_provider("deepseek", base_url="x2", model="y2", api_key="sk-1")
    cfg = svc.get_app_config()
    assert cfg.llm_providers["deepseek"].is_default is False
    assert cfg.llm_providers["kimi"].is_default is True
    # Edit kimi (currently default); flag must stay True.
    svc.set_llm_provider("kimi", base_url="x3", model="y3", api_key="sk-2")
    cfg = svc.get_app_config()
    assert cfg.llm_providers["kimi"].is_default is True


def test_cannot_demote_only_default_via_update(svc: ConfigService) -> None:
    """is_default=False on the only default is silently ignored — leaving
    the dict with zero defaults breaks the invariant."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1", is_default=False)
    assert svc.get_default_llm_provider() == "deepseek"


def test_delete_default_promotes_first_survivor(svc: ConfigService) -> None:
    """Deleting the current default with survivors must promote one so the
    invariant 'while non-empty, exactly one default' holds."""
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2")
    svc.set_llm_provider("qwen", base_url="x", model="y", api_key="sk-3")
    assert svc.get_default_llm_provider() == "deepseek"
    svc.delete_llm_provider("deepseek")
    new_default = svc.get_default_llm_provider()
    assert new_default in {"kimi", "qwen"}
    cfg = svc.get_app_config()
    assert cfg.llm_providers[new_default].is_default is True


def test_delete_non_default_leaves_existing_default(svc: ConfigService) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y", api_key="sk-2")
    svc.delete_llm_provider("kimi")
    assert svc.get_default_llm_provider() == "deepseek"


def test_delete_last_provider_clears_default(svc: ConfigService) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.delete_llm_provider("deepseek")
    assert svc.get_default_llm_provider() is None


def test_get_default_llm_provider_returns_none_when_unconfigured(svc: ConfigService) -> None:
    assert svc.get_default_llm_provider() is None
