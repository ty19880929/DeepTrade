"""LLM Manager — framework-level service for multi-provider LLM access.

DESIGN §0.7 + §10. The manager is the **only** path plugins should use to
obtain an ``LLMClient``. It provides:

    * ``list_providers()``      — names of currently usable providers
    * ``get_provider_info()``   — display metadata (no api_key)
    * ``get_client()``          — a fully-wired ``LLMClient`` for one provider

Multiple providers coexist; there is no "default" provider concept. A single
plugin may call ``get_client("deepseek", ...)`` and ``get_client("kimi", ...)``
in the same run and treat them as independent clients.

Thread safety
-------------
**Not thread-safe.** Cached ``LLMClient`` instances are shared by callers
holding the same ``LLMManager`` and asking for the same
``(name, plugin_id, run_id)`` triple. The underlying ``OpenAI`` SDK + httpx
pool is itself thread-safe, but ``LLMClient.complete_json()`` writes to the
shared DB connection (``llm_calls`` audit) and ``llm_calls.jsonl`` file —
those are **not** serialized inside the client. Callers wanting parallel
LLM calls must either:

    * use a separate ``LLMManager`` per worker thread, or
    * serialize their ``complete_json`` calls externally with a lock.

Inside a single-threaded plugin run (the default), caching strictly improves
performance (one transport per provider) with no correctness risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from deeptrade.core.llm_client import LLMClient, OpenAIClientTransport

if TYPE_CHECKING:  # pragma: no cover
    from deeptrade.core.config import ConfigService
    from deeptrade.core.db import Database


class LLMNotConfiguredError(RuntimeError):
    """Raised when a provider is not configured, or its ``api_key`` is missing.

    Distinct from a generic ``KeyError`` so callers can branch on this
    specifically (e.g. CLI returns a friendly hint pointing at
    ``deeptrade config set-llm``).
    """


@dataclass(frozen=True)
class LLMProviderInfo:
    """Display metadata for a provider; intentionally excludes ``api_key``
    so this can be safely logged or shown in a TUI.
    """

    name: str
    model: str
    base_url: str


_CacheKey = tuple[str, str, str | None]


class LLMManager:
    """Framework-level LLM access for plugins.

    Construction is cheap (no network IO, no client building); the actual
    ``OpenAI`` transport is created lazily on the first ``get_client()`` for
    a given ``(name, plugin_id, run_id)`` and then cached on this manager.
    """

    def __init__(self, db: Database, config: ConfigService) -> None:
        self._db = db
        self._config = config
        self._cache: dict[_CacheKey, LLMClient] = {}

    # ------------------------------------------------------------------
    # Listing / introspection
    # ------------------------------------------------------------------

    def list_providers(self) -> list[str]:
        """Names of providers that are configured AND have an api_key set.

        Filtering by api_key prevents callers from receiving a name that
        will 401 at the first ``complete_json`` call. Returned list is
        sorted for determinism.
        """
        cfg = self._config.get_app_config()
        out: list[str] = []
        for name in sorted(cfg.llm_providers.keys()):
            if self._config.get(f"llm.{name}.api_key"):
                out.append(name)
        return out

    def get_provider_info(self, name: str) -> LLMProviderInfo:
        """Return display metadata for ``name``.

        Raises ``LLMNotConfiguredError`` if the provider is not in
        ``llm.providers``. Does NOT check that an api_key is set — this
        method is intentionally usable for inspecting partially-configured
        entries (e.g. listing for an "edit existing" CLI flow).
        """
        cfg = self._config.get_app_config()
        provider = cfg.llm_providers.get(name)
        if provider is None:
            raise LLMNotConfiguredError(
                f"LLM provider {name!r} is not configured; "
                "run `deeptrade config set-llm` to add it"
            )
        return LLMProviderInfo(name=name, model=provider.model, base_url=provider.base_url)

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    def get_client(
        self,
        name: str,
        *,
        plugin_id: str,
        run_id: str | None = None,
        reports_dir: Path | None = None,
    ) -> LLMClient:
        """Return an ``LLMClient`` bound to provider ``name``.

        Cached by ``(name, plugin_id, run_id)`` for the lifetime of this
        manager — repeated calls during a single run reuse the same
        transport / httpx pool.

        Raises:
            LLMNotConfiguredError — provider not in ``llm.providers``, or
                its ``llm.<name>.api_key`` is unset.
        """
        cache_key: _CacheKey = (name, plugin_id, run_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        cfg = self._config.get_app_config()
        provider = cfg.llm_providers.get(name)
        if provider is None:
            raise LLMNotConfiguredError(
                f"LLM provider {name!r} is not configured; "
                "run `deeptrade config set-llm` to add it"
            )
        api_key = self._config.get(f"llm.{name}.api_key")
        if not api_key:
            raise LLMNotConfiguredError(
                f"LLM provider {name!r} has no api_key set; "
                f"run `deeptrade config set-llm` and choose {name!r}"
            )

        transport = OpenAIClientTransport(
            api_key=str(api_key),
            base_url=provider.base_url,
            timeout=provider.timeout,
        )
        # v0.7 — LLMClient no longer holds a profile set; the per-call
        # ``StageProfile`` is supplied by the plugin at ``complete_json`` time.
        client = LLMClient(
            self._db,
            transport,
            model=provider.model,
            plugin_id=plugin_id,
            run_id=run_id,
            audit_full_payload=cfg.llm_audit_full_payload,
            reports_dir=reports_dir,
        )
        self._cache[cache_key] = client
        return client
