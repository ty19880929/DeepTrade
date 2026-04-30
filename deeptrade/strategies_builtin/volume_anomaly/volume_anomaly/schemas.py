"""Pydantic schemas for the volume-anomaly LLM stage.

Hard constraints (mirrors limit_up_board conventions):
    * extra='forbid' on every model
    * candidate_id round-trips verbatim from input
    * rank is a dense permutation 1..N within each batch
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VAEvidenceItem(BaseModel):
    """One field-level fact the LLM is using to reason.

    `field` MUST refer to a key actually present in the input prompt; `unit`
    is REQUIRED so prompt and model speak the same units.
    """

    model_config = ConfigDict(extra="forbid")
    field: str = Field(..., min_length=1, max_length=64)
    value: str | int | float | None
    unit: str = Field(..., min_length=1, max_length=16)
    interpretation: str = Field(..., min_length=1, max_length=120)


class VATrendCandidate(BaseModel):
    """One LLM verdict per input candidate."""

    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    ts_code: str
    name: str
    rank: int = Field(ge=1)
    launch_score: float = Field(ge=0, le=100)
    confidence: Literal["high", "medium", "low"]
    prediction: Literal["imminent_launch", "watching", "not_yet"]
    pattern: Literal[
        "breakout",
        "consolidation_break",
        "first_wave",
        "second_leg",
        "unclear",
    ]
    washout_quality: Literal["sufficient", "partial", "insufficient", "unclear"]
    rationale: str = Field(..., max_length=200)
    key_evidence: list[VAEvidenceItem] = Field(min_length=1, max_length=5)
    next_session_watch: list[str] = Field(min_length=1, max_length=4)
    invalidation_triggers: list[str] = Field(min_length=1, max_length=4)
    risk_flags: list[str] = Field(default_factory=list, max_length=5)
    missing_data: list[str] = Field(default_factory=list)


class VATrendResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["continuation_prediction"]
    trade_date: str
    next_trade_date: str
    batch_no: int = Field(ge=1)
    batch_total: int = Field(ge=1)
    market_context_summary: str = Field(..., max_length=200)
    risk_disclaimer: str = Field(..., max_length=160)
    candidates: list[VATrendCandidate]

    @field_validator("candidates")
    @classmethod
    def ranks_must_be_dense_1_to_n(cls, v: list[VATrendCandidate]) -> list[VATrendCandidate]:
        """Per-batch rank must be a dense permutation 1..N."""
        ranks = sorted(c.rank for c in v)
        expected = list(range(1, len(ranks) + 1))
        if ranks != expected:
            raise ValueError(f"candidate ranks must be a dense permutation 1..N; got {ranks}")
        return v
