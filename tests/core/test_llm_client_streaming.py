"""v0.9 — OpenAICompatTransport streaming wire-shape regression.

The transport switched from ``stream=False`` (single blocking response) to
``stream=True`` + ``stream_options={"include_usage": True}`` to dodge the
intermediate-gateway idle-timeout that killed long Moonshot-thinking calls.
These tests pin down:

    * chunk concatenation + final-chunk usage pickup
    * empty content (thinking model burned the budget) returns ``text=""``
      so the upper layer can raise ``LLMEmptyResponseError`` itself
    * transport errors during create() *and* mid-iteration both surface
      as ``LLMTransportError`` (tenacity retries them)
    * missing usage on the final chunk records 0/0 rather than crashing

Plugin / audit / retry layers are unaffected and tested elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from openai import APITimeoutError

from deeptrade.core.llm_client import (
    GenericOpenAITransport,
    LLMTransportError,
)


def _text_chunk(content: str | None) -> Any:
    delta = SimpleNamespace(content=content, role=None)
    choice = SimpleNamespace(delta=delta, index=0, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=None)


def _final_usage_chunk(*, prompt_tokens: int, completion_tokens: int) -> Any:
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _make_transport(stream_chunks: list[Any]) -> GenericOpenAITransport:
    """Build a GenericOpenAITransport whose openai client returns the given
    chunk sequence on chat.completions.create(). Bypasses __init__ so we
    don't construct a real OpenAI client (no API key required)."""
    t = GenericOpenAITransport.__new__(GenericOpenAITransport)
    t._client = MagicMock()
    t._client.chat.completions.create.return_value = iter(stream_chunks)
    return t


class TestStreamingHappyPath:
    def test_concatenates_chunks_and_picks_up_final_usage(self) -> None:
        t = _make_transport(
            [
                _text_chunk(None),  # role-only opener
                _text_chunk('{"items":'),
                _text_chunk('[{"code":"000001","score":7}]'),
                _text_chunk("}"),
                _final_usage_chunk(prompt_tokens=50, completion_tokens=20),
            ]
        )
        resp = t.chat(
            model="m",
            system="s",
            user="u",
            temperature=1.0,
            max_tokens=512,
            thinking=False,
            reasoning_effort="medium",
        )
        assert resp.text == '{"items":[{"code":"000001","score":7}]}'
        assert resp.input_tokens == 50
        assert resp.output_tokens == 20

    def test_passes_stream_true_and_include_usage(self) -> None:
        t = _make_transport([_final_usage_chunk(prompt_tokens=1, completion_tokens=1)])
        t.chat(
            model="m",
            system="s",
            user="u",
            temperature=1.0,
            max_tokens=64,
            thinking=False,
            reasoning_effort="medium",
        )
        kwargs = t._client.chat.completions.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        # M3 hard constraint — no tools, ever.
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
        assert "functions" not in kwargs


class TestStreamingEmptyContent:
    def test_no_content_chunks_yields_empty_text(self) -> None:
        """Thinking model burns its budget on internal reasoning before
        emitting any visible content. The transport itself does NOT raise —
        it honestly returns ``text=""``; ``LLMClient._with_retry`` is what
        promotes that into ``LLMEmptyResponseError``."""
        t = _make_transport(
            [
                _text_chunk(None),  # role-only
                _final_usage_chunk(prompt_tokens=50, completion_tokens=2048),
            ]
        )
        resp = t.chat(
            model="m",
            system="s",
            user="u",
            temperature=1.0,
            max_tokens=2048,
            thinking=False,
            reasoning_effort="medium",
        )
        assert resp.text == ""
        assert resp.output_tokens == 2048


class TestStreamingErrors:
    def test_timeout_during_create_wraps_to_LLMTransportError(self) -> None:
        t = GenericOpenAITransport.__new__(GenericOpenAITransport)
        t._client = MagicMock()
        t._client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
        with pytest.raises(LLMTransportError):
            t.chat(
                model="m",
                system="s",
                user="u",
                temperature=1.0,
                max_tokens=64,
                thinking=False,
                reasoning_effort="medium",
            )

    def test_timeout_during_iteration_wraps_to_LLMTransportError(self) -> None:
        """Errors mid-stream (connection reset after headers, gateway drop
        between chunks) must also surface as LLMTransportError so tenacity
        retries — otherwise the partial bytes leak as an opaque exception."""

        def raising_iter() -> Any:
            yield _text_chunk('{"items":[')
            raise APITimeoutError(request=MagicMock())

        t = GenericOpenAITransport.__new__(GenericOpenAITransport)
        t._client = MagicMock()
        t._client.chat.completions.create.return_value = raising_iter()
        with pytest.raises(LLMTransportError):
            t.chat(
                model="m",
                system="s",
                user="u",
                temperature=1.0,
                max_tokens=64,
                thinking=False,
                reasoning_effort="medium",
            )


class TestStreamingUsageMissing:
    def test_missing_usage_records_zero_not_raise(self) -> None:
        """In-scope providers all populate usage on the final chunk when
        ``include_usage`` is set, but the transport must not crash if a
        provider omits it — it just records 0/0 and lets the call return."""
        t = _make_transport(
            [
                _text_chunk("ok"),
                SimpleNamespace(choices=[], usage=None),  # final chunk, no usage
            ]
        )
        resp = t.chat(
            model="m",
            system="s",
            user="u",
            temperature=1.0,
            max_tokens=64,
            thinking=False,
            reasoning_effort="medium",
        )
        assert resp.text == "ok"
        assert resp.input_tokens == 0
        assert resp.output_tokens == 0
