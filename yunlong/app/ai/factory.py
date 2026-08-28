# -*- coding: utf-8 -*-
"""AIProvider 工厂：基于配置构建对应实现。

推荐方案：统一使用 LiteLLM 调用，更换模型仅改 yaml 三行。
"""

from __future__ import annotations

from .base import AIProvider
from .litellm_provider import LiteLLMProvider
from ..core.config import AIConfig


def build_ai_provider(cfg: AIConfig) -> AIProvider:
    """根据配置生成 AIProvider。

    目前所有模型都通过 LiteLLM 接入，实现「改配置即换模型」。
    未来如需独立实现（如自研模型），可在此分支。
    """
    return LiteLLMProvider(
        provider=cfg.provider,
        api_key=cfg.api_key,
        model=cfg.model,
        base_url=cfg.base_url,
    )
