"""v0.6 — LLMManager: list / info / get_client + cache + missing-config errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeptrade.core.config import ConfigService
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.llm_client import LLMClient
from deeptrade.core.llm_manager import LLMManager, LLMNotConfiguredError, LLMProviderInfo
from deeptrade.core.secrets import SecretStore


@pytest.fixture
def svc(tmp_path: Path) -> ConfigService:
    db = Database(tmp_path / "test.duckdb")
    apply_core_migrations(db)
    return ConfigService(db, secret_store=SecretStore(db, force_plaintext=True))


@pytest.fixture
def mgr(svc: ConfigService) -> LLMManager:
    return LLMManager(svc._db, svc)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


def test_list_providers_empty_when_unconfigured(mgr: LLMManager) -> None:
    assert mgr.list_providers() == []


def test_list_providers_filters_out_missing_api_key(svc: ConfigService, mgr: LLMManager) -> None:
    """A provider entry without an api_key must NOT appear — semantics is
    'usable providers', not 'declared providers'.
    """
    svc.set_llm_provider("deepseek", base_url="x", model="y", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="x", model="y")  # api_key missing
    assert mgr.list_providers() == ["deepseek"]


def test_list_providers_returns_sorted(svc: ConfigService, mgr: LLMManager) -> None:
    svc.set_llm_provider("zeta", base_url="x", model="y", api_key="sk-z")
    svc.set_llm_provider("alpha", base_url="x", model="y", api_key="sk-a")
    svc.set_llm_provider("mu", base_url="x", model="y", api_key="sk-m")
    assert mgr.list_providers() == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# get_provider_info
# ---------------------------------------------------------------------------


def test_get_provider_info_returns_metadata(svc: ConfigService, mgr: LLMManager) -> None:
    svc.set_llm_provider(
        "deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="sk-x",
    )
    info = mgr.get_provider_info("deepseek")
    assert info == LLMProviderInfo(
        name="deepseek", model="deepseek-v4-pro", base_url="https://api.deepseek.com"
    )


def test_get_provider_info_for_missing_provider_raises(mgr: LLMManager) -> None:
    with pytest.raises(LLMNotConfiguredError, match="not configured"):
        mgr.get_provider_info("does-not-exist")


def test_get_provider_info_works_without_api_key(svc: ConfigService, mgr: LLMManager) -> None:
    """info() is intentionally usable for entries without api_key — supports
    'edit existing' UX flows."""
    svc.set_llm_provider("kimi", base_url="x", model="y")
    info = mgr.get_provider_info("kimi")
    assert info.name == "kimi"


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


def test_get_client_returns_llm_client_for_configured_provider(
    svc: ConfigService, mgr: LLMManager
) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="m", api_key="sk-1")
    client = mgr.get_client("deepseek", plugin_id="some-plugin", run_id="run-1")
    assert isinstance(client, LLMClient)


def test_get_client_caches_by_name_plugin_runid(svc: ConfigService, mgr: LLMManager) -> None:
    svc.set_llm_provider("deepseek", base_url="x", model="m", api_key="sk-1")
    a = mgr.get_client("deepseek", plugin_id="P", run_id="R")
    b = mgr.get_client("deepseek", plugin_id="P", run_id="R")
    assert a is b
    # Different plugin → different instance (audit isolation)
    c = mgr.get_client("deepseek", plugin_id="Q", run_id="R")
    assert c is not a
    # Different run_id → different instance
    d = mgr.get_client("deepseek", plugin_id="P", run_id="R2")
    assert d is not a


def test_get_client_raises_when_provider_missing(mgr: LLMManager) -> None:
    with pytest.raises(LLMNotConfiguredError, match="not configured"):
        mgr.get_client("nope", plugin_id="P")


def test_get_client_raises_when_api_key_missing(svc: ConfigService, mgr: LLMManager) -> None:
    svc.set_llm_provider("kimi", base_url="x", model="m")  # no api_key
    with pytest.raises(LLMNotConfiguredError, match="api_key"):
        mgr.get_client("kimi", plugin_id="P")


# ---------------------------------------------------------------------------
# Multi-provider parallel use case
# ---------------------------------------------------------------------------


def test_multiple_providers_coexist(svc: ConfigService, mgr: LLMManager) -> None:
    """The defining v0.6 use case: a plugin holding two clients at once."""
    svc.set_llm_provider("deepseek", base_url="u1", model="m1", api_key="sk-1")
    svc.set_llm_provider("kimi", base_url="u2", model="m2", api_key="sk-2")

    a = mgr.get_client("deepseek", plugin_id="P", run_id="R")
    b = mgr.get_client("kimi", plugin_id="P", run_id="R")

    assert a is not b
    # Each is independently a working LLMClient
    assert isinstance(a, LLMClient)
    assert isinstance(b, LLMClient)


# ---------------------------------------------------------------------------
# Transport routing — base_url decides the OpenAI-compat subclass
# ---------------------------------------------------------------------------


def test_get_client_uses_dashscope_transport_for_dashscope_base_url(
    svc: ConfigService, mgr: LLMManager
) -> None:
    """A qwen-plus provider configured against DashScope must end up wired to
    DashScopeTransport so ``StageProfile.thinking=False`` actually disables
    qwen3's default-on thinking mode (otherwise long-output batches time out).
    """
    from deeptrade.core.llm_client import DashScopeTransport

    svc.set_llm_provider(
        "qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.6-plus",
        api_key="sk-x",
    )
    client = mgr.get_client("qwen-plus", plugin_id="P", run_id="R")
    assert isinstance(client._transport, DashScopeTransport)  # type: ignore[attr-defined]


def test_get_client_uses_generic_transport_for_unknown_base_url(
    svc: ConfigService, mgr: LLMManager
) -> None:
    """Anything not in the routing table falls back to GenericOpenAITransport;
    pre-existing providers (DeepSeek/Kimi/...) keep their v0.6 behavior."""
    from deeptrade.core.llm_client import GenericOpenAITransport

    svc.set_llm_provider("deepseek", base_url="https://api.deepseek.com", model="m", api_key="sk-1")
    client = mgr.get_client("deepseek", plugin_id="P", run_id="R")
    assert isinstance(client._transport, GenericOpenAITransport)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# v0.8 — get_client(name=None) resolves the framework default
# ---------------------------------------------------------------------------


def test_get_client_with_none_resolves_default_provider(
    svc: ConfigService, mgr: LLMManager
) -> None:
    """Non-debate plugins call get_client(plugin_id=...) without a name —
    the manager picks the entry with is_default=True."""
    svc.set_llm_provider("kimi", base_url="u1", model="m1", api_key="sk-1")
    svc.set_llm_provider("deepseek", base_url="u2", model="m2", api_key="sk-2", is_default=True)
    default_client = mgr.get_client(plugin_id="P", run_id="R")
    explicit_client = mgr.get_client("deepseek", plugin_id="P", run_id="R")
    # Cached by resolved name → same instance for both call shapes.
    assert default_client is explicit_client


def test_get_client_with_none_raises_when_no_provider_configured(mgr: LLMManager) -> None:
    with pytest.raises(LLMNotConfiguredError, match="No default LLM provider"):
        mgr.get_client(plugin_id="P", run_id="R")


def test_get_client_with_none_raises_when_default_api_key_missing(
    svc: ConfigService, mgr: LLMManager
) -> None:
    """The default exists but has no api_key — surfaces the same friendly
    error path as get_client('name') would."""
    svc.set_llm_provider("deepseek", base_url="x", model="m")  # no api_key
    with pytest.raises(LLMNotConfiguredError, match="api_key"):
        mgr.get_client(plugin_id="P", run_id="R")
