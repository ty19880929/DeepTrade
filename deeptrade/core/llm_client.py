"""LLM client (OpenAI-compatible protocol, multi-provider).

v0.7 — stage 概念彻底退出框架。``complete_json`` 不再认识 stage 名字，
由调用方直接传入一个已解析好的 ``StageProfile``。预设档（``fast/balanced/
quality``）保留为框架级用户配置（``app.profile``），但**预设档 → 各 stage
tuning 的映射表归插件自己维护**（详见 ``deeptrade.plugins_api.llm``）。

v0.6 — renamed from ``deepseek_client.py`` to reflect framework-level role.
The same client now backs every provider configured in ``llm.providers``
(DeepSeek / Qwen / Kimi / Doubao / GLM / Yi / SiliconFlow / OpenRouter, ...)
— they all speak the OpenAI Chat Completions wire format. Real heterogeneous
protocols (Anthropic native, Gemini native) will land later as a separate
transport plugin type; this module assumes OpenAI-compatible.

Construction is not normally done by plugins directly — use
``deeptrade.core.llm_manager.LLMManager.get_client(name, ...)``.

DESIGN §10.2 + the M3/F3/F5 hard constraints from v0.3 review:

    * **No tool calls EVER** — ``chat.completions.create()`` is invoked
      without any ``tools`` / ``tool_choice`` / ``functions`` parameter, and
      plugins are not handed a ``chat_with_tools`` surface.
    * **Caller-supplied profile**: each ``complete_json`` call carries a
      ``StageProfile`` (thinking / reasoning_effort / temperature /
      max_output_tokens). Framework does not look up profiles by stage name.
    * **JSON-only**: ``response_format={"type": "json_object"}`` + Pydantic
      double-validate; one retry on JSON / Pydantic failure, then re-raise.
    * **Audit**: each call is persisted to ``llm_calls`` (request_json,
      response_json, prompt_hash, validation_status, plugin_id).

Transports:
    OpenAICompatTransport  — base class; OpenAI-compatible chat completion API.
        Subclasses ``_provider_extra_body()`` to translate ``StageProfile.thinking``
        into each provider's wire shape (DashScope's ``enable_thinking`` boolean,
        Claude-on-OAI-compat's ``thinking={"type":"enabled"}``, etc.).
    GenericOpenAITransport — providers without a thinking dial (Kimi/DeepSeek/
        OpenRouter/...); ``thinking`` flag is silently dropped per the
        plugins_api/llm.py contract.
    DashScopeTransport     — Alibaba Qwen on DashScope. qwen3.x defaults to
        thinking=ON, so we MUST pass ``enable_thinking`` explicitly for both
        True and False — otherwise the plugin's "thinking off" preset is
        ineffective and runs hit the per-call timeout.
    RecordedTransport      — tests; FIFO-replays canned responses.

Routing: ``_select_transport_class(base_url)`` picks the right subclass via a
small framework-internal substring table. Unknown base_urls fall back to
GenericOpenAITransport. Insertion of a new provider type = one entry in
``_TRANSPORT_BY_BASE_URL`` + one subclass; configuration-layer schemas are
unaffected on purpose (the dialect is not user-tunable).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from deeptrade.core.db import Database
from deeptrade.plugins_api.llm import StageProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for LLM client errors."""


class LLMTransportError(LLMError):
    """Network / SDK error — transient, retried by tenacity."""


class LLMValidationError(LLMError):
    """JSON parse or Pydantic validation failure."""


class LLMEmptyResponseError(LLMValidationError):
    """The model returned no visible content (``message.content`` was empty
    or None). Distinct from a JSON parse error so callers can show a clearer
    message and the retry path can append a "skip extended reasoning" hint
    instead of resending the same prompt that already produced nothing."""


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Normalized response shape regardless of transport."""

    text: str
    input_tokens: int
    output_tokens: int


class LLMTransport(ABC):
    """Carrier for one chat completion call."""

    @abstractmethod
    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        thinking: bool,
        reasoning_effort: str,
    ) -> LLMResponse:
        """Send one chat. MUST NOT pass tools/tool_choice/functions."""


class OpenAICompatTransport(LLMTransport):
    """Production transport base — OpenAI-compatible chat completion.

    Subclasses override ``_provider_extra_body()`` to inject provider-specific
    knobs (notably the various flavors of the "thinking" dial). The default
    implementation returns ``{}``: appropriate for OpenAI-compatible providers
    that don't recognize a thinking concept, where ``StageProfile.thinking``
    is silently dropped per the plugins_api/llm.py contract.

    v0.6 — ``supports_reasoning_effort`` (class attribute) gates whether
    ``reasoning_effort`` from :class:`StageProfile` is forwarded to the
    provider. Default ``False``: most OpenAI-compatible Chinese providers
    (DeepSeek / Kimi / Qwen non-reasoning models / Doubao) either ignore
    the field or reject it as a 400, so we drop it by default. Subclasses
    override to ``True`` only when the provider has documented support.
    """

    # v0.6 H5 — see class docstring. The default is False; OpenAI-official
    # is the only known transport that flips it to True.
    supports_reasoning_effort: bool = False

    def __init__(self, api_key: str, base_url: str, timeout: int) -> None:
        from openai import OpenAI  # noqa: PLC0415

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def _provider_extra_body(self, *, thinking: bool) -> dict[str, Any]:
        """Provider-specific keys merged into ``extra_body``. Default: none."""
        del thinking  # base class has no provider knobs
        return {}

    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        thinking: bool,
        reasoning_effort: str,
    ) -> LLMResponse:
        from openai import APIError, APITimeoutError  # noqa: PLC0415

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # v0.6 H5 — only send ``reasoning_effort`` when the transport
        # declares support AND the caller actually supplied a non-empty
        # value. Sending it unconditionally was the v0.5 default and is
        # the dominant failure mode on Chinese OpenAI-compat providers.
        if self.supports_reasoning_effort and reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        extra_body = self._provider_extra_body(thinking=thinking)
        if extra_body:
            kwargs["extra_body"] = extra_body

        # ⚠ HARD CONSTRAINT (M3): we MUST NOT pass tools/tool_choice/functions.
        # If a future maintainer adds them, the no-tools test in V0.5 fails.

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except (APITimeoutError, APIError) as e:
            raise LLMTransportError(str(e)) from e

        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return LLMResponse(
            text=text,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )


class GenericOpenAITransport(OpenAICompatTransport):
    """OpenAI-compatible providers without a thinking dial.

    Catch-all for providers like DeepSeek, Kimi, Doubao, GLM, Yi, OpenRouter, …
    The ``thinking`` flag from ``StageProfile`` is silently dropped here per
    the contract documented in ``plugins_api/llm.py``.
    """


class DashScopeTransport(OpenAICompatTransport):
    """Alibaba DashScope (Qwen family).

    qwen3.x defaults to thinking=ON; the only way to actually disable it is to
    send ``enable_thinking=False`` explicitly. Sending nothing leaves thinking
    on, which can blow past the per-call timeout for high-output prompts (the
    model burns its budget on internal reasoning before any visible content).
    Therefore we always emit ``enable_thinking`` — both for True and False.
    """

    def _provider_extra_body(self, *, thinking: bool) -> dict[str, Any]:
        return {"enable_thinking": thinking}


class OpenAIOfficialTransport(OpenAICompatTransport):
    """OpenAI's own ``api.openai.com`` endpoint.

    Only this transport documents support for the ``reasoning_effort``
    parameter (on the o1 / o3 reasoning family); flipping the base-class
    flag here makes ``OpenAICompatTransport.chat`` forward the value from
    :class:`StageProfile`. Other OpenAI-compatible providers either ignore
    the field or 400 on it, so they keep the base-class default (False).
    """

    supports_reasoning_effort = True


# ---------------------------------------------------------------------------
# Transport routing (framework-internal)
# ---------------------------------------------------------------------------

# base_url substring → transport class. Substring (not strict host) is fine
# because each entry's pattern is anchored to the provider's well-known domain
# and tolerates port / path / version variations. New entries land here and
# nowhere else; user-facing config has no "dialect" knob on purpose.
_TRANSPORT_BY_BASE_URL: tuple[tuple[str, type[OpenAICompatTransport]], ...] = (
    ("dashscope.aliyuncs.com", DashScopeTransport),
    ("api.openai.com", OpenAIOfficialTransport),
)


def _select_transport_class(base_url: str) -> type[OpenAICompatTransport]:
    """Pick the OpenAI-compat transport subclass for a provider's base_url.

    Substring match against ``_TRANSPORT_BY_BASE_URL``; unknown base_urls fall
    back to ``GenericOpenAITransport`` (which silently drops thinking flags).
    """
    for pattern, cls in _TRANSPORT_BY_BASE_URL:
        if pattern in base_url:
            return cls
    return GenericOpenAITransport


class RecordedTransport(LLMTransport):
    """Test transport — FIFO-replays pre-registered responses.

    Use ``register(response)`` to seed; each ``chat()`` call pops one entry.
    """

    def __init__(self) -> None:
        self._queue: list[LLMResponse | Exception] = []
        # last-seen kwargs the test can introspect (M3 no-tools test relies on this)
        self.last_call_kwargs: dict[str, Any] = {}

    def register(self, response: LLMResponse | Exception) -> None:
        self._queue.append(response)

    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        thinking: bool,
        reasoning_effort: str,
    ) -> LLMResponse:
        self.last_call_kwargs = {
            "model": model,
            "system": system,
            "user": user,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "thinking": thinking,
            "reasoning_effort": reasoning_effort,
        }
        if not self._queue:
            raise LLMTransportError("RecordedTransport: no more queued responses")
        entry = self._queue.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    def __init__(
        self,
        db: Database,
        transport: LLMTransport,
        *,
        model: str,
        plugin_id: str | None = None,
        run_id: str | None = None,
        audit_full_payload: bool = False,
        reports_dir: Path | None = None,
    ) -> None:
        self._db = db
        self._transport = transport
        self._model = model
        self._plugin_id = plugin_id
        self._run_id = run_id
        # F-M2 — when False (default), DB rows keep just hash + response excerpt;
        # full payloads ALWAYS go to reports_dir/llm_calls.jsonl (set by caller).
        self._audit_full = audit_full_payload
        self._reports_dir = reports_dir

    # --- main entry ----------------------------------------------------

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        profile: StageProfile,
        envelope_defaults: dict[str, Any] | None = None,
    ) -> tuple[BaseModel, dict[str, Any]]:
        """Send a JSON-mode chat and validate against `schema`.

        ``profile`` (required) — caller-resolved per-call tuning. Plugins
        own the preset → stage profile mapping; the framework just consumes
        the four fields (thinking, reasoning_effort, temperature,
        max_output_tokens).

        ``envelope_defaults`` (optional) — top-level keys to inject when
        the LLM omits them. Useful for caller-controlled metadata like
        ``stage`` / ``trade_date`` / ``batch_no``. Only fills keys missing
        from the parsed payload, never overwrites.

        Returns (validated_model, meta) where meta includes input_tokens,
        output_tokens, latency_ms, prompt_hash. Raises:
            LLMValidationError    — JSON or Pydantic still failing after 1 retry
            LLMEmptyResponseError — model returned empty content twice
            LLMTransportError     — transport-level error after retries
        """
        return self._with_retry(system, user, schema, profile, envelope_defaults)

    @staticmethod
    def _retry_hint_for(error: Exception) -> str:
        """Pick a corrective hint for the second attempt based on what failed.
        Re-sending the identical prompt after a known-bad response is wasted
        budget; the hint nudges the model toward the specific failure mode."""
        if isinstance(error, LLMEmptyResponseError):
            return (
                "\n\n⚠ 上一次响应为空。请直接输出符合 schema 的 JSON，"
                "不要进行扩展推理或 markdown 包裹；只返回最终的 JSON 对象。"
            )
        if isinstance(error, json.JSONDecodeError):
            return (
                "\n\n⚠ 上一次响应不是合法 JSON。请只返回 JSON 对象，"
                "不要使用代码块标记 ``` 或前后缀。"
            )
        if isinstance(error, ValidationError):
            return (
                "\n\n⚠ 上一次响应缺少必填字段或字段值非法。请严格按照系统消息中的"
                "【输出格式】填写每一个字段，不要省略 ts_code / score / strength_level "
                "等枚举值字段，evidence 内每条 4 个字段不可省。"
            )
        return ""

    @retry(
        retry=retry_if_exception_type(LLMTransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _transport_call(self, system: str, user: str, profile: StageProfile) -> LLMResponse:
        return self._transport.chat(
            model=self._model,
            system=system,
            user=user,
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            thinking=profile.thinking,
            reasoning_effort=profile.reasoning_effort,
        )

    def _with_retry(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        profile: StageProfile,
        envelope_defaults: dict[str, Any] | None = None,
    ) -> tuple[BaseModel, dict[str, Any]]:
        """Two attempts: one retry on JSON / Pydantic / empty-response failure.

        Empty responses (``response.text`` blank) are detected separately:
        the second-attempt user prompt gets an appended hint asking the
        model to emit JSON directly without extended reasoning, since
        re-sending the identical prompt has zero new information.
        """
        prompt_hash = hashlib.sha256((system + user).encode("utf-8")).hexdigest()

        last_err: Exception | None = None
        last_response: LLMResponse | None = None
        current_user = user
        for attempt in (1, 2):
            t0 = time.monotonic()
            response = self._transport_call(system, current_user, profile)
            latency_ms = int((time.monotonic() - t0) * 1000)
            last_response = response

            # Detect empty content BEFORE trying to parse — the JSON error
            # otherwise surfaces as a misleading "Expecting value at line 1
            # column 1 (char 0)" with no signal that the model returned
            # nothing visible at all.
            empty = (response.text or "").strip() == ""
            try:
                if empty:
                    raise LLMEmptyResponseError(
                        f"model returned empty content "
                        f"(input_tokens={response.input_tokens}, "
                        f"output_tokens={response.output_tokens}, latency_ms={latency_ms}); "
                        "common causes: extended reasoning consumed the output budget, "
                        "max_output_tokens too low, or model-side content filter."
                    )
                payload = json.loads(response.text)
                if envelope_defaults and isinstance(payload, dict):
                    for k, v in envelope_defaults.items():
                        payload.setdefault(k, v)
                obj = schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError, LLMEmptyResponseError) as e:
                last_err = e
                self._record_call(
                    prompt_hash=prompt_hash,
                    request_system=system,
                    request_user=current_user,
                    response_text=response.text,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=latency_ms,
                    validation_status="retry" if attempt == 1 else "failed",
                    error=f"{type(e).__name__}: {e}",
                )
                # On retry, attach a corrective hint tailored to the failure
                # mode. Same prompt twice is wasted budget.
                if attempt == 1:
                    current_user = user + self._retry_hint_for(e)
                continue
            else:
                self._record_call(
                    prompt_hash=prompt_hash,
                    request_system=system,
                    request_user=current_user,
                    response_text=response.text,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=latency_ms,
                    validation_status="ok",
                    error=None,
                )
                return obj, {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "latency_ms": latency_ms,
                    "prompt_hash": prompt_hash,
                }

        # both attempts failed — preserve the specific error subclass so callers
        # (and tests) can branch on whether the model returned empty vs. invalid.
        assert last_err is not None
        tail = (last_response.text if last_response else "")[:200]
        msg = f"validation failed after retry; last error: {last_err}; last response (truncated): {tail}"
        if isinstance(last_err, LLMEmptyResponseError):
            raise LLMEmptyResponseError(msg) from last_err
        raise LLMValidationError(msg) from last_err

    # --- audit log ----------------------------------------------------

    def _record_call(
        self,
        *,
        prompt_hash: str,
        request_system: str,
        request_user: str,
        response_text: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        validation_status: str,
        error: str | None,
    ) -> None:
        call_id = str(uuid.uuid4())
        # F-M2 — write FULL payload to reports/<run_id>/llm_calls.jsonl regardless
        # of audit_full_payload (DB lean is just a storage optimization, not an
        # audit gap). The DB row may be lean or full per audit_full_payload.
        self._append_jsonl(
            call_id=call_id,
            prompt_hash=prompt_hash,
            request_system=request_system,
            request_user=request_user,
            response_text=response_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            validation_status=validation_status,
            error=error,
        )
        if self._audit_full:
            request_json = json.dumps(
                {"system": request_system, "user": request_user},
                ensure_ascii=False,
            )
            response_payload = response_text
        else:
            # Lean mode: store user-prompt size + first 200 chars of response.
            request_json = json.dumps(
                {
                    "system_len": len(request_system),
                    "user_len": len(request_user),
                    "audit": "lean",
                },
                ensure_ascii=False,
            )
            response_payload = (response_text or "")[:200]
        self._db.execute(
            "INSERT INTO llm_calls(call_id, run_id, plugin_id, model, prompt_hash, "
            "input_tokens, output_tokens, latency_ms, request_json, response_json, "
            "validation_status, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                self._run_id,
                self._plugin_id,
                self._model,
                prompt_hash,
                input_tokens,
                output_tokens,
                latency_ms,
                request_json,
                response_payload,
                validation_status,
                error,
            ),
        )

    def _append_jsonl(
        self,
        *,
        call_id: str,
        prompt_hash: str,
        request_system: str,
        request_user: str,
        response_text: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        validation_status: str,
        error: str | None,
    ) -> None:
        """F-M2 — write the FULL prompt/response to llm_calls.jsonl always.
        DB lean-mode is purely a storage optimization; audit must be reproducible
        from the jsonl file in the report directory."""
        if self._reports_dir is None:
            return
        try:
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            with (self._reports_dir / "llm_calls.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "call_id": call_id,
                            "run_id": self._run_id,
                            "plugin_id": self._plugin_id,
                            "model": self._model,
                            "prompt_hash": prompt_hash,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "latency_ms": latency_ms,
                            "system": request_system,
                            "user": request_user,
                            "response": response_text,
                            "validation_status": validation_status,
                            "error": error,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as e:  # noqa: BLE001 — never let audit IO crash the run
            logger.warning("failed to write llm_calls.jsonl: %s", e)
