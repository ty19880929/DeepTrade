"""LLM调参契约 — 插件作者唯一需要 import 的 LLM 调参类型。

v0.7：Stage 概念彻底退出框架。框架的 ``LLMClient.complete_json`` 不再认识
stage 名字，由调用方直接传入一个已解析好的 ``StageProfile`` 实例；预设档名
（``fast / balanced / quality``）保留为框架级用户配置（``app.profile``），
但**预设档 → 各 stage tuning 的映射表归插件自己维护**。

插件作者用法：

    from deeptrade.plugins_api import StageProfile

    PROFILES: dict[str, dict[str, StageProfile]] = {
        "fast":     {"my_stage": StageProfile(thinking=False, ...)},
        "balanced": {"my_stage": StageProfile(thinking=True,  ...)},
        "quality":  {"my_stage": StageProfile(thinking=True,  ...)},
    }

    def resolve_profile(preset: str, stage: str) -> StageProfile:
        return PROFILES[preset][stage]

随后在 pipeline 中：

    prof = resolve_profile(rt.config.get_app_config().app_profile, "my_stage")
    obj, _ = llm.complete_json(system=..., user=..., schema=..., profile=prof)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StageProfile(BaseModel):
    """单次 LLM 调用的调参档：四字段对应 OpenAI Chat Completions 协议参数。

    ``thinking`` / ``reasoning_effort`` 在不支持思维链的 provider 上由 transport
    静默丢弃（DeepSeek/Qwen/Kimi/Doubao/GLM/... 各家支持情况不同）。
    """

    model_config = ConfigDict(extra="forbid")

    thinking: bool
    reasoning_effort: Literal["low", "medium", "high"]
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(ge=1024, le=384_000)
