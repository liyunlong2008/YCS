# -*- coding: utf-8 -*-
"""AIProvider 工厂：基于配置构建对应实现。

推荐方案：统一使用 LiteLLM 调用，更换模型仅改 yaml 三行。
密钥占位或强制离线时，返回 OfflineFallbackAIProvider，避免拖慢主循环。
"""

from __future__ import annotations

from loguru import logger

from .base import AIProvider, OfflineFallbackAIProvider
from .litellm_provider import LiteLLMProvider
from ..core.config import AIConfig
from ..core.safety import _is_placeholder as __ai_ph


def build_ai_provider(cfg: AIConfig, *, force_offline: bool = False) -> AIProvider:
    """根据配置生成 AIProvider。

    目前所有模型都通过 LiteLLM 接入，实现「改配置即换模型」。
    未来如需独立实现（如自研模型），可在此分支。

    Args:
        cfg: 来自 config.yaml 的 AI 配置。
        force_offline: 为 True 时直接返回离线回退 Provider（用于 fixture 调试、部署强制离线）。
    """
    placeholder = __ai_ph((cfg.api_key or "").strip())
    if force_offline or placeholder:
        why = ("force_offline" if force_offline else "ai.api_key 是占位值")
        logger.warning("[AI] 使用 OfflineFallbackAIProvider：{}", why)
        return OfflineFallbackAIProvider(
            reason=f"OfflineFallbackAI: {why}（返回保守 LOW_VOLATILITY，不触发交易）"
        )
    return LiteLLMProvider(
        provider=cfg.provider,
        api_key=cfg.api_key,
        model=cfg.model,
        base_url=cfg.base_url,
        timeout_seconds=float(cfg.timeout_seconds),
        max_retries=int(cfg.max_retries),
        thinking_mode=str(cfg.thinking_mode),
        enable_stream=bool(cfg.enable_stream),
    )
