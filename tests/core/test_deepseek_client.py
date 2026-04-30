"""V0.4 DoD — DeepSeekClient: profile / stage / no-tools / JSON validate / retry / audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from deeptrade.core.config import PROFILES_DEFAULT
from deeptrade.core.db import Database, apply_core_migrations
from deeptrade.core.deepseek_client import (
    KNOWN_STAGES,
    DeepSeekClient,
    LLMResponse,
    LLMTransport,
    LLMTransportError,
    LLMUnknownStageError,
    LLMValidationError,
    OpenAIClientTransport,
    RecordedTransport,
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
def client(db: Database, transport: RecordedTransport) -> DeepSeekClient:
    return DeepSeekClient(
        db,
        transport,
        model="deepseek-v4-pro",
        profiles=PROFILES_DEFAULT["balanced"],
        plugin_id="test-plugin",
        run_id=None,
    )


# Convenience helper
def _ok_response(stage: str, n: int = 2) -> LLMResponse:
    payload = {
        "stage": stage,
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
    client: DeepSeekClient, transport: RecordedTransport
) -> None:
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    obj, meta = client.complete_json(
        system="sys",
        user="usr",
        schema=_SchemaResp,
        stage="strong_target_analysis",
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
    client: DeepSeekClient, transport: RecordedTransport, db: Database
) -> None:
    transport.register(
        "strong_target_analysis",
        LLMResponse(text="not-json{}", input_tokens=10, output_tokens=4),
    )
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    obj, _ = client.complete_json(
        system="sys", user="usr", schema=_SchemaResp, stage="strong_target_analysis"
    )
    assert isinstance(obj, _SchemaResp)
    # llm_calls log shows: 1 retry + 1 ok
    rows = db.fetchall("SELECT validation_status FROM llm_calls ORDER BY created_at")
    assert [r[0] for r in rows] == ["retry", "ok"]


# ---------------------------------------------------------------------------
# DoD 3 — Pydantic validation error → 1 retry then ok
# ---------------------------------------------------------------------------


def test_complete_json_retries_once_on_pydantic_error(
    client: DeepSeekClient, transport: RecordedTransport
) -> None:
    bad_payload = json.dumps({"stage": "strong_target_analysis", "candidates": [{"x": 1}]})
    transport.register(
        "strong_target_analysis",
        LLMResponse(text=bad_payload, input_tokens=10, output_tokens=10),
    )
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    obj, _ = client.complete_json(
        system="sys", user="usr", schema=_SchemaResp, stage="strong_target_analysis"
    )
    assert isinstance(obj, _SchemaResp)


def test_complete_json_raises_after_two_failures(
    client: DeepSeekClient, transport: RecordedTransport, db: Database
) -> None:
    bad = LLMResponse(text="garbage", input_tokens=1, output_tokens=1)
    transport.register("strong_target_analysis", bad)
    transport.register("strong_target_analysis", bad)
    with pytest.raises(LLMValidationError):
        client.complete_json(
            system="sys", user="usr", schema=_SchemaResp, stage="strong_target_analysis"
        )
    rows = db.fetchall("SELECT validation_status FROM llm_calls ORDER BY created_at")
    assert [r[0] for r in rows] == ["retry", "failed"]


# ---------------------------------------------------------------------------
# DoD 4 — Stage-specific max_output_tokens (F5 fix)
# ---------------------------------------------------------------------------


def test_stage_specific_max_output_tokens_r1_32k_final_8k(
    client: DeepSeekClient, transport: RecordedTransport
) -> None:
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    client.complete_json(system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis")
    assert transport.last_call_kwargs["max_tokens"] == 32768

    transport.register("final_ranking", _ok_response("final_ranking"))
    client.complete_json(system="s", user="u", schema=_SchemaResp, stage="final_ranking")
    assert transport.last_call_kwargs["max_tokens"] == 8192


# ---------------------------------------------------------------------------
# DoD 5 — Fast profile disables thinking for ALL stages (F3 fix)
# ---------------------------------------------------------------------------


def test_fast_profile_disables_thinking_for_all_stages(
    db: Database, transport: RecordedTransport
) -> None:
    cli = DeepSeekClient(db, transport, model="deepseek-v4-pro", profiles=PROFILES_DEFAULT["fast"])
    for stage in KNOWN_STAGES:
        transport.register(stage, _ok_response(stage))
        cli.complete_json(system="s", user="u", schema=_SchemaResp, stage=stage)
        assert transport.last_call_kwargs["thinking"] is False, (
            f"fast profile must disable thinking; stage={stage}"
        )


def test_balanced_profile_disables_r1_only(db: Database, transport: RecordedTransport) -> None:
    cli = DeepSeekClient(
        db, transport, model="deepseek-v4-pro", profiles=PROFILES_DEFAULT["balanced"]
    )
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    cli.complete_json(system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis")
    assert transport.last_call_kwargs["thinking"] is False

    transport.register("continuation_prediction", _ok_response("continuation_prediction"))
    cli.complete_json(system="s", user="u", schema=_SchemaResp, stage="continuation_prediction")
    assert transport.last_call_kwargs["thinking"] is True


def test_quality_profile_enables_thinking_for_all(
    db: Database, transport: RecordedTransport
) -> None:
    cli = DeepSeekClient(
        db, transport, model="deepseek-v4-pro", profiles=PROFILES_DEFAULT["quality"]
    )
    for stage in KNOWN_STAGES:
        transport.register(stage, _ok_response(stage))
        cli.complete_json(system="s", user="u", schema=_SchemaResp, stage=stage)
        assert transport.last_call_kwargs["thinking"] is True


# ---------------------------------------------------------------------------
# DoD 6 — NO `tools` ever passed (M3 hard constraint)
# ---------------------------------------------------------------------------


def test_no_tools_param_passed_through_transport(
    client: DeepSeekClient, transport: RecordedTransport
) -> None:
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    client.complete_json(system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis")
    forbidden = {"tools", "tool_choice", "functions", "function_call"}
    assert not (forbidden & set(transport.last_call_kwargs.keys()))


def test_openai_transport_signature_does_not_accept_tools() -> None:
    """OpenAIClientTransport.chat() must NOT have a `tools` kwarg in its
    signature; this prevents accidentally adding it later."""
    import inspect

    sig = inspect.signature(OpenAIClientTransport.chat)
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
    client: DeepSeekClient, transport: RecordedTransport, db: Database
) -> None:
    """M4: by default audit is LEAN — DB row carries hash + truncated payload only.
    Full prompt/response always go to reports/<run_id>/llm_calls.jsonl."""
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    client.complete_json(
        system="my-system", user="my-user", schema=_SchemaResp, stage="strong_target_analysis"
    )
    row = db.fetchone(
        "SELECT stage, model, prompt_hash, input_tokens, output_tokens, "
        "validation_status, request_json, response_json FROM llm_calls"
    )
    assert row is not None
    stage, model, ph, in_t, out_t, vs, req_json, resp_json = row
    assert stage == "strong_target_analysis"
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
    cli = DeepSeekClient(
        db,
        transport,
        model="deepseek-v4-pro",
        profiles=PROFILES_DEFAULT["balanced"],
        audit_full_payload=True,
    )
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    cli.complete_json(
        system="full-system",
        user="full-user",
        schema=_SchemaResp,
        stage="strong_target_analysis",
    )
    row = db.fetchone("SELECT request_json FROM llm_calls")
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed["system"] == "full-system"
    assert parsed["user"] == "full-user"


# ---------------------------------------------------------------------------
# DoD 8 — RecordedTransport replays deterministically
# ---------------------------------------------------------------------------


def test_recorded_transport_replays_response_deterministically(
    db: Database, transport: RecordedTransport
) -> None:
    cli = DeepSeekClient(
        db, transport, model="deepseek-v4-pro", profiles=PROFILES_DEFAULT["balanced"]
    )
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis", n=3))
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis", n=5))

    obj1, _ = cli.complete_json(
        system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis"
    )
    obj2, _ = cli.complete_json(
        system="s2", user="u2", schema=_SchemaResp, stage="strong_target_analysis"
    )
    assert len(obj1.candidates) == 3
    assert len(obj2.candidates) == 5


# ---------------------------------------------------------------------------
# DoD 9 — unknown stage raises
# ---------------------------------------------------------------------------


def test_unknown_stage_raises(client: DeepSeekClient) -> None:
    with pytest.raises(LLMUnknownStageError):
        client.complete_json(system="s", user="u", schema=_SchemaResp, stage="bogus_stage_name")


# ---------------------------------------------------------------------------
# DoD 10 — Transport error retried by tenacity
# ---------------------------------------------------------------------------


def test_transport_error_retried_by_tenacity(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DeepSeekClient._transport_call.retry,  # type: ignore[attr-defined]
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
            return _ok_response("strong_target_analysis")

    cli = DeepSeekClient(
        db, Flaky(), model="deepseek-v4-pro", profiles=PROFILES_DEFAULT["balanced"]
    )
    obj, _ = cli.complete_json(
        system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis"
    )
    assert isinstance(obj, _SchemaResp)


# ---------------------------------------------------------------------------
# DoD 11 — Reasoning effort wired per stage
# ---------------------------------------------------------------------------


def test_reasoning_effort_per_stage(client: DeepSeekClient, transport: RecordedTransport) -> None:
    transport.register("strong_target_analysis", _ok_response("strong_target_analysis"))
    client.complete_json(system="s", user="u", schema=_SchemaResp, stage="strong_target_analysis")
    assert transport.last_call_kwargs["reasoning_effort"] == "medium"

    transport.register("continuation_prediction", _ok_response("continuation_prediction"))
    client.complete_json(system="s", user="u", schema=_SchemaResp, stage="continuation_prediction")
    assert transport.last_call_kwargs["reasoning_effort"] == "high"
