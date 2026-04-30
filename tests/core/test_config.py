"""V0.2 DoD — ConfigService priority + secret routing + profile loading."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from deeptrade.core.config import (
    PROFILES_DEFAULT,
    SECRET_KEYS,
    AppConfig,
    ConfigService,
    env_var_for,
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
    assert svc.get("deepseek.profile") == "balanced"
    # db override
    svc.set("deepseek.profile", "quality")
    assert svc.get("deepseek.profile") == "quality"
    # env var beats db
    monkeypatch.setenv(env_var_for("deepseek.profile"), "fast")
    assert svc.get("deepseek.profile") == "fast"
    assert svc.source_of("deepseek.profile") == "env"


def test_env_var_naming_convention() -> None:
    assert env_var_for("tushare.token") == "DEEPTRADE_TUSHARE_TOKEN"
    assert env_var_for("deepseek.api_key") == "DEEPTRADE_DEEPSEEK_API_KEY"
    assert env_var_for("app.close_after") == "DEEPTRADE_APP_CLOSE_AFTER"


# --- DoD 3 & 4: secret vs non-secret routing -------------------------------


def test_set_secret_routes_to_secret_store_only(svc: ConfigService) -> None:
    svc.set("deepseek.api_key", "sk-abc")
    # app_config table must NOT contain the secret
    rows = svc._db.fetchall("SELECT key FROM app_config WHERE key='deepseek.api_key'")  # type: ignore[attr-defined]
    assert rows == []
    # secret_store DOES
    rows = svc._db.fetchall("SELECT key FROM secret_store WHERE key='deepseek.api_key'")  # type: ignore[attr-defined]
    assert rows and rows[0][0] == "deepseek.api_key"
    assert svc.get("deepseek.api_key") == "sk-abc"


def test_set_non_secret_routes_to_app_config_only(svc: ConfigService) -> None:
    svc.set("tushare.rps", 10.0)
    rows = svc._db.fetchall("SELECT value_json FROM app_config WHERE key='tushare.rps'")  # type: ignore[attr-defined]
    assert rows and rows[0][0] == "10.0"
    # secret_store stays empty
    rows = svc._db.fetchall("SELECT key FROM secret_store WHERE key='tushare.rps'")  # type: ignore[attr-defined]
    assert rows == []


def test_secret_keys_constant_is_complete() -> None:
    """Sanity: all keys ending in `.token` or `.api_key` should be in SECRET_KEYS."""
    assert "tushare.token" in SECRET_KEYS
    assert "deepseek.api_key" in SECRET_KEYS


# --- DoD 5: invalid value rejection ----------------------------------------


def test_set_unknown_key_raises(svc: ConfigService) -> None:
    with pytest.raises(ValueError, match="unknown config key"):
        svc.set("unknown.key", "x")


def test_set_invalid_profile_value_raises(svc: ConfigService) -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError varies
        svc.set("deepseek.profile", "ultra")


# --- DoD 6: close_after default = 18:00 (S2 fix) --------------------------


def test_close_after_default_is_18_00(svc: ConfigService) -> None:
    cfg = svc.get_app_config()
    assert cfg.app_close_after == time(18, 0)


# --- DoD 7: close_after can be overridden ---------------------------------


def test_close_after_can_be_overridden(svc: ConfigService) -> None:
    svc.set("app.close_after", "17:30")
    cfg = svc.get_app_config()
    assert cfg.app_close_after == time(17, 30)


# --- profile loading ------------------------------------------------------


def test_profile_default_balanced_disables_thinking_for_r1_only(svc: ConfigService) -> None:
    p = svc.get_profile()
    assert p.strong_target_analysis.thinking is False
    assert p.continuation_prediction.thinking is True
    assert p.final_ranking.thinking is True


def test_fast_profile_disables_thinking_for_all_stages() -> None:
    """F3 fix: fast profile must have thinking=false everywhere."""
    p = PROFILES_DEFAULT["fast"]
    assert p.strong_target_analysis.thinking is False
    assert p.continuation_prediction.thinking is False
    assert p.final_ranking.thinking is False


def test_stage_max_output_tokens_r1_r2_32k_final_8k() -> None:
    """F5 fix: R1/R2 default to 32k output, final_ranking 8k."""
    for name in ("fast", "balanced", "quality"):
        p = PROFILES_DEFAULT[name]
        assert p.strong_target_analysis.max_output_tokens == 32768
        assert p.continuation_prediction.max_output_tokens == 32768
        assert p.final_ranking.max_output_tokens == 8192


def test_unknown_profile_name_raises(svc: ConfigService) -> None:
    with pytest.raises(ValueError, match="unknown deepseek profile"):
        svc.get_profile("ultra")


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
