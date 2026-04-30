"""DeepSeek V4 client.

DESIGN §10.2 + the M3/F3/F5 hard constraints from v0.3 review:

    * **No tool calls EVER** — `chat.completions.create()` is invoked without
      any `tools` / `tool_choice` / `functions` parameter, and StrategyContext
      does not expose a `chat_with_tools` surface.
    * **Stage-aware profiles**: thinking / reasoning_effort / temperature /
      max_output_tokens are per-stage (`fast` / `balanced` / `quality`).
    * **JSON-only**: `response_format={"type": "json_object"}` + Pydantic
      double-validate; one retry on JSON / Pydantic failure, then re-raise.
    * **Audit**: each call is persisted to `llm_calls` (request_json,
      response_json, prompt_hash, validation_status).

Transports:
    OpenAIClientTransport  — production; OpenAI-compatible chat completion API
    RecordedTransport      — tests; replays a canned response for a given key
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

from deeptrade.core.config import DeepSeekProfileSet, StageProfile
from deeptrade.core.db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage namespace
# ---------------------------------------------------------------------------

KNOWN_STAGES = frozenset({"strong_target_analysis", "continuation_prediction", "final_ranking"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for DeepSeek client errors."""


class LLMTransportError(LLMError):
    """Network / SDK error — transient, retried by tenacity."""


class LLMValidationError(LLMError):
    """JSON parse or Pydantic validation failure."""


class LLMEmptyResponseError(LLMValidationError):
    """The model returned no visible content (``message.content`` was empty
    or None). Distinct from a JSON parse error so callers can show a clearer
    message and the retry path can append a "skip extended reasoning" hint
    instead of resending the same prompt that already produced nothing."""


class LLMUnknownStageError(LLMError):
    """Caller passed a stage name not in KNOWN_STAGES."""


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


class OpenAIClientTransport(LLMTransport):
    """Production transport — uses the OpenAI SDK pointed at DeepSeek."""

    def __init__(self, api_key: str, base_url: str, timeout: int) -> None:
        from openai import OpenAI  # noqa: PLC0415

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

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

        extra_body: dict[str, Any] = {}
        if thinking:
            extra_body["thinking"] = {"type": "enabled"}

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
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


class RecordedTransport(LLMTransport):
    """Test transport — replays canned responses keyed by stage / system / user.

    Use ``register(stage, response)`` to seed; matching is by stage when no
    per-call recording exists, falling back to FIFO replay.
    """

    def __init__(self) -> None:
        self._by_stage: dict[str, list[LLMResponse | Exception]] = {}
        # last-seen kwargs the test can introspect (M3 no-tools test relies on this)
        self.last_call_kwargs: dict[str, Any] = {}

    def register(self, stage: str, response: LLMResponse | Exception) -> None:
        self._by_stage.setdefault(stage, []).append(response)

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
        # The stage label is woven into the system prompt by the production
        # caller. For tests, we extract it heuristically: in V0.4 we'll trust
        # the test to pre-register by looking at last seeded stage in order.
        # Concretely: tests register via `register(stage, ...)` and call
        # complete_json with a matching stage; the client passes through to
        # this transport which pops one entry from the most-recent stage.
        # To support stage-keyed replay reliably, the client re-injects the
        # `stage` via a thread-local set just before chat(). We use that.
        self.last_call_kwargs = {
            "model": model,
            "system": system,
            "user": user,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "thinking": thinking,
            "reasoning_effort": reasoning_effort,
        }
        stage = _CURRENT_STAGE.get("v")
        queue = self._by_stage.get(stage or "", [])
        if not queue:
            # fallback to any stage with remaining entries
            for _s, q in self._by_stage.items():
                if q:
                    queue = q
                    break
        if not queue:
            raise LLMTransportError(f"no recorded response for stage={stage!r}")
        entry = queue.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


# Module-level holder so RecordedTransport can introspect the stage. Kept
# small because tests run sequentially.
_CURRENT_STAGE: dict[str, str | None] = {"v": None}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DeepSeekClient:
    def __init__(
        self,
        db: Database,
        transport: LLMTransport,
        *,
        model: str,
        profiles: DeepSeekProfileSet,
        plugin_id: str | None = None,
        run_id: str | None = None,
        audit_full_payload: bool = False,
        reports_dir: Path | None = None,
    ) -> None:
        self._db = db
        self._transport = transport
        self._model = model
        self._profiles = profiles
        self._plugin_id = plugin_id
        self._run_id = run_id
        # F-M2 — when False (default), DB rows keep just hash + response excerpt;
        # full payloads ALWAYS go to reports_dir/llm_calls.jsonl (set by caller).
        self._audit_full = audit_full_payload
        self._reports_dir = reports_dir

    # --- stage profile lookup ------------------------------------------

    def _stage_profile(self, stage: str) -> StageProfile:
        if stage not in KNOWN_STAGES:
            raise LLMUnknownStageError(f"unknown stage: {stage!r}")
        return getattr(self._profiles, stage)

    # --- main entry ----------------------------------------------------

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        stage: str,
        envelope_defaults: dict[str, Any] | None = None,
    ) -> tuple[BaseModel, dict[str, Any]]:
        """Send a JSON-mode chat and validate against `schema`.

        ``envelope_defaults`` (optional) — top-level keys to inject when
        the LLM omits them. Useful for framework-controlled metadata like
        ``stage`` / ``trade_date`` / ``batch_no`` that the LLM has no
        business deciding; the framework already knows them. Only fills
        keys missing from the parsed payload, never overwrites.

        Returns (validated_model, meta) where meta includes input_tokens,
        output_tokens, latency_ms, prompt_hash. Raises:
            LLMUnknownStageError  — bad stage name
            LLMValidationError    — JSON or Pydantic still failing after 1 retry
            LLMTransportError     — transport-level error after retries
        """
        cfg = self._stage_profile(stage)

        # Track stage for RecordedTransport
        _CURRENT_STAGE["v"] = stage
        try:
            obj, meta = self._with_retry(system, user, schema, stage, cfg, envelope_defaults)
        finally:
            _CURRENT_STAGE["v"] = None
        return obj, meta

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
    def _transport_call(self, system: str, user: str, cfg: StageProfile) -> LLMResponse:
        return self._transport.chat(
            model=self._model,
            system=system,
            user=user,
            temperature=cfg.temperature,
            max_tokens=cfg.max_output_tokens,
            thinking=cfg.thinking,
            reasoning_effort=cfg.reasoning_effort,
        )

    def _with_retry(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        stage: str,
        cfg: StageProfile,
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
            response = self._transport_call(system, current_user, cfg)
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
                    stage=stage,
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
                    stage=stage,
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
                    "stage": stage,
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
        stage: str,
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
            stage=stage,
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
            "INSERT INTO llm_calls(call_id, run_id, plugin_id, stage, model, prompt_hash, "
            "input_tokens, output_tokens, latency_ms, request_json, response_json, "
            "validation_status, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                self._run_id,
                self._plugin_id,
                stage,
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
        stage: str,
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
                            "stage": stage,
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
