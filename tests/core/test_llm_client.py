"""V0.4 DoD — LLMClient: profile / no-tools / JSON validate / retry / audit.

(Renamed from ``test_deepseek_client.py`` in v0.6; profiles 在 v0.7 改由调用方
直接传入 ``StageProfile``，stage 概念退出框架。)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.llm_client import (
    DashScopeTransport,
    GenericOpenAITransport,
    LLMClient,
    LLMResponse,
    LLMTransport,
    LLMTransportError,
    LLMValidationError,
    OpenAICompatTransport,
    RecordedTransport,
    _select_transport_class,
)
from deeptrade.plugins_api import StageProfile

# Test stage profiles — mirror what plugin profiles.py would resolve.
# `THINKING_OFF_32K` corresponds to fast/balanced R1; `THINKING_ON_32K` to
# balanced/quality R2; `THINKING_ON_8K` to balanced/quality final_ranking.
THINKING_OFF_32K = StageProfile(
    thinking=False, reasoning_effort="medium", temperature=0.1, max_output_tokens=32768
)
THINKING_ON_32K = StageProfile(
    thinking=True, reasoning_effort="high", temperature=0.2, max_output_tokens=32768
)
THINKING_ON_8K = StageProfile(
    thinking=True, reasoning_effort="high", temperature=0.0, max_output_tokens=8192
)


class _SchemaCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    selected: bool
    score: float = Field(ge=0, le=100)


class _SchemaResp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    candidates: list[_SchemaCandidate]


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.duckdb")
    apply_core_migrations(d)
    return d


@pytest.fixture
def transport() -> RecordedTransport:
    return RecordedTransport()


@pytest.fixture
def client(db: Database, transport: RecordedTransport) -> LLMClient:
    return LLMClient(
        db,
        transport,
        model="deepseek-v4-pro",
        plugin_id="test-plugin",
        run_id=None,
    )


def _ok_response(stage_label: str = "test", n: int = 2) -> LLMResponse:
    payload = {
        "stage": stage_label,
        "candidates": [
            {"candidate_id": f"c{i}", "selected": i % 2 == 0, "score": 50.0 + i} for i in range(n)
        ],
    }
    text = json.dumps(payload, ensure_ascii=False)
    return LLMResponse(text=text, input_tokens=120, output_tokens=80)


# ---------------------------------------------------------------------------
# DoD 1 — JSON validates with Pydantic
# ---------------------------------------------------------------------------


def test_complete_json_validates_with_pydantic(
    client: LLMClient, transport: RecordedTransport
) -> None:
    transport.register(_ok_response())
    obj, meta = client.complete_json(
        system="sys", user="usr", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    assert isinstance(obj, _SchemaResp)
    assert len(obj.candidates) == 2
    assert meta["input_tokens"] == 120
    assert meta["output_tokens"] == 80
    assert "prompt_hash" in meta and len(meta["prompt_hash"]) == 64


# ---------------------------------------------------------------------------
# DoD 2 — JSON decode error → 1 retry then ok
# ---------------------------------------------------------------------------


def test_complete_json_retries_once_on_json_decode_error(
    client: LLMClient, transport: RecordedTransport, db: Database
) -> None:
    transport.register(LLMResponse(text="not-json{}", input_tokens=10, output_tokens=4))
    transport.register(_ok_response())
    obj, _ = client.complete_json(
        system="sys", user="usr", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    assert isinstance(obj, _SchemaResp)
    rows = db.fetchall("SELECT validation_status FROM llm_calls ORDER BY created_at")
    assert [r[0] for r in rows] == ["retry", "ok"]


# ---------------------------------------------------------------------------
# DoD 3 — Pydantic validation error → 1 retry then ok
# ---------------------------------------------------------------------------


def test_complete_json_retries_once_on_pydantic_error(
    client: LLMClient, transport: RecordedTransport
) -> None:
    bad_payload = json.dumps({"stage": "x", "candidates": [{"x": 1}]})
    transport.register(LLMResponse(text=bad_payload, input_tokens=10, output_tokens=10))
    transport.register(_ok_response())
    obj, _ = client.complete_json(
        system="sys", user="usr", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    assert isinstance(obj, _SchemaResp)


def test_complete_json_raises_after_two_failures(
    client: LLMClient, transport: RecordedTransport, db: Database
) -> None:
    bad = LLMResponse(text="garbage", input_tokens=1, output_tokens=1)
    transport.register(bad)
    transport.register(bad)
    with pytest.raises(LLMValidationError):
        client.complete_json(system="sys", user="usr", schema=_SchemaResp, profile=THINKING_OFF_32K)
    rows = db.fetchall("SELECT validation_status FROM llm_calls ORDER BY created_at")
    assert [r[0] for r in rows] == ["retry", "failed"]


# ---------------------------------------------------------------------------
# DoD 4 — Per-call max_output_tokens (from caller-supplied profile)
# ---------------------------------------------------------------------------


def test_per_call_max_output_tokens(
    client: LLMClient, transport: RecordedTransport
) -> None:
    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K)
    assert transport.last_call_kwargs["max_tokens"] == 32768

    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_ON_8K)
    assert transport.last_call_kwargs["max_tokens"] == 8192


# ---------------------------------------------------------------------------
# DoD 5 — Profile.thinking flag is wired through the transport
# ---------------------------------------------------------------------------


def test_thinking_off_passes_thinking_false(
    client: LLMClient, transport: RecordedTransport
) -> None:
    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K)
    assert transport.last_call_kwargs["thinking"] is False


def test_thinking_on_passes_thinking_true(
    client: LLMClient, transport: RecordedTransport
) -> None:
    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_ON_32K)
    assert transport.last_call_kwargs["thinking"] is True


# ---------------------------------------------------------------------------
# DoD 6 — NO `tools` ever passed (M3 hard constraint)
# ---------------------------------------------------------------------------


def test_no_tools_param_passed_through_transport(
    client: LLMClient, transport: RecordedTransport
) -> None:
    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K)
    forbidden = {"tools", "tool_choice", "functions", "function_call"}
    assert not (forbidden & set(transport.last_call_kwargs.keys()))


def test_openai_transport_signature_does_not_accept_tools() -> None:
    """OpenAICompatTransport.chat() must NOT have a `tools` kwarg in its
    signature; this prevents accidentally adding it later."""
    import inspect

    sig = inspect.signature(OpenAICompatTransport.chat)
    forbidden = {"tools", "tool_choice", "functions", "function_call"}
    assert not (forbidden & set(sig.parameters.keys()))


def test_llm_transport_abc_has_no_tool_methods() -> None:
    """LLMTransport surface must not expose `chat_with_tools` etc. (M3)."""
    forbidden_method_names = {"chat_with_tools", "register_tool", "use_tool"}
    members = {m for m in dir(LLMTransport) if not m.startswith("_")}
    assert not (forbidden_method_names & members)


# ---------------------------------------------------------------------------
# DoD 7 — llm_calls audit log (request, response, prompt_hash)
# ---------------------------------------------------------------------------


def test_complete_json_persists_llm_calls_record(
    client: LLMClient, transport: RecordedTransport, db: Database
) -> None:
    """M4: by default audit is LEAN — DB row carries hash + truncated payload only.
    Full prompt/response always go to reports/<run_id>/llm_calls.jsonl."""
    transport.register(_ok_response())
    client.complete_json(
        system="my-system", user="my-user", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    row = db.fetchone(
        "SELECT model, prompt_hash, input_tokens, output_tokens, "
        "validation_status, request_json, response_json FROM llm_calls"
    )
    assert row is not None
    model, ph, in_t, out_t, vs, req_json, resp_json = row
    assert model == "deepseek-v4-pro"
    assert len(ph) == 64
    assert in_t == 120
    assert out_t == 80
    assert vs == "ok"
    parsed_req = json.loads(req_json)
    assert parsed_req.get("audit") == "lean"
    assert parsed_req["system_len"] == len("my-system")
    assert parsed_req["user_len"] == len("my-user")
    # Response is truncated to <=200 chars but still present
    assert "candidates" in resp_json


def test_complete_json_full_audit_mode_keeps_payload(
    db: Database, transport: RecordedTransport
) -> None:
    """M4: opt-in full mode keeps original system/user/response in the row."""
    cli = LLMClient(
        db,
        transport,
        model="deepseek-v4-pro",
        audit_full_payload=True,
    )
    transport.register(_ok_response())
    cli.complete_json(
        system="full-system", user="full-user", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    row = db.fetchone("SELECT request_json FROM llm_calls")
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed["system"] == "full-system"
    assert parsed["user"] == "full-user"


# ---------------------------------------------------------------------------
# DoD 8 — RecordedTransport replays FIFO
# ---------------------------------------------------------------------------


def test_recorded_transport_replays_fifo(
    db: Database, transport: RecordedTransport
) -> None:
    cli = LLMClient(db, transport, model="deepseek-v4-pro")
    transport.register(_ok_response(n=3))
    transport.register(_ok_response(n=5))

    obj1, _ = cli.complete_json(
        system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    obj2, _ = cli.complete_json(
        system="s2", user="u2", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    assert len(obj1.candidates) == 3
    assert len(obj2.candidates) == 5


# ---------------------------------------------------------------------------
# DoD 9 — Transport error retried by tenacity
# ---------------------------------------------------------------------------


def test_transport_error_retried_by_tenacity(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LLMClient._transport_call.retry,  # type: ignore[attr-defined]
        "sleep",
        lambda *_: None,
    )

    class Flaky(LLMTransport):
        def __init__(self) -> None:
            self.fail_left = 2

        def chat(self, **kw: Any) -> LLMResponse:  # type: ignore[override]
            if self.fail_left > 0:
                self.fail_left -= 1
                raise LLMTransportError("503")
            return _ok_response()

    cli = LLMClient(db, Flaky(), model="deepseek-v4-pro")
    obj, _ = cli.complete_json(
        system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K
    )
    assert isinstance(obj, _SchemaResp)


# ---------------------------------------------------------------------------
# DoD 10 — reasoning_effort flows through from profile to transport
# ---------------------------------------------------------------------------


def test_reasoning_effort_flows_through(client: LLMClient, transport: RecordedTransport) -> None:
    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_OFF_32K)
    assert transport.last_call_kwargs["reasoning_effort"] == "medium"

    transport.register(_ok_response())
    client.complete_json(system="s", user="u", schema=_SchemaResp, profile=THINKING_ON_32K)
    assert transport.last_call_kwargs["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Provider-specific thinking dialect (transport hierarchy)
# ---------------------------------------------------------------------------


def test_generic_transport_drops_thinking_silently() -> None:
    """Catch-all for OpenAI-compat providers without a thinking dial — the
    flag is dropped per the plugins_api/llm.py contract."""
    t = GenericOpenAITransport(
        api_key="dummy", base_url="https://api.deepseek.com", timeout=10
    )
    assert t._provider_extra_body(thinking=True) == {}
    assert t._provider_extra_body(thinking=False) == {}


def test_dashscope_transport_always_emits_enable_thinking() -> None:
    """DashScope qwen3.x defaults to thinking=ON; the framework MUST send
    `enable_thinking=False` explicitly to disable it. Sending only on True
    (or using Anthropic's `thinking={"type":"enabled"}` shape) leaves
    thinking on, which manifests as request timeouts on long-output prompts.
    """
    t = DashScopeTransport(
        api_key="dummy",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=10,
    )
    assert t._provider_extra_body(thinking=True) == {"enable_thinking": True}
    assert t._provider_extra_body(thinking=False) == {"enable_thinking": False}


def test_dashscope_transport_sends_enable_thinking_through_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end wire-shape regression — the kwargs handed to OpenAI's
    chat.completions.create() must carry `extra_body={"enable_thinking": ...}`
    for DashScope, even when thinking=False."""
    from types import SimpleNamespace

    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        choice = SimpleNamespace(message=SimpleNamespace(content='{"k": 1}'))
        return SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    t = DashScopeTransport(
        api_key="dummy",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=10,
    )
    monkeypatch.setattr(t._client.chat.completions, "create", fake_create)

    t.chat(
        model="qwen3.6-plus",
        system="s",
        user="u",
        temperature=0.1,
        max_tokens=128,
        thinking=False,
        reasoning_effort="medium",
    )
    assert captured["extra_body"] == {"enable_thinking": False}


def test_select_transport_class_routes_dashscope_by_base_url() -> None:
    assert (
        _select_transport_class("https://dashscope.aliyuncs.com/compatible-mode/v1")
        is DashScopeTransport
    )


def test_select_transport_class_defaults_to_generic() -> None:
    """Unknown base_urls fall back to GenericOpenAITransport — this preserves
    the v0.6 behavior (no thinking knob) for every previously-supported
    provider, so no migration of stored configs is required."""
    assert _select_transport_class("https://api.deepseek.com") is GenericOpenAITransport
    assert _select_transport_class("https://api.openai.com/v1") is GenericOpenAITransport
    assert _select_transport_class("https://api.moonshot.cn/v1") is GenericOpenAITransport
