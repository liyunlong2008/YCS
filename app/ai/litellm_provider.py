# -*- coding: utf-8 -*-
"""基于 LiteLLM 的统一 AI 网关实现。

支持：DeepSeek / OpenAI / Claude / Gemini / OpenRouter / Qwen / Grok / Mistral 等。
更换模型：仅修改 config.yaml 三行，无需改代码。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ..core.constants import MarketRegime
from .base import AIProvider, MarketAnalysisResult, MarketData


class LiteLLMProvider(AIProvider):
    """统一 LiteLLM AI 实现。"""

    # 提示词模板：严格要求 JSON 输出
    SYSTEM_PROMPT = (
        "你是专业的加密货币市场分析师，只负责输出市场状态，不给出交易建议。"
        "请基于给定的 K 线和量价数据，输出严格 JSON 格式："
        '{"market_regime": "...", "confidence": 0-100, "reason": "中文理由"}。'
        f"market_regime 仅可选：{', '.join(m.value for m in MarketRegime)}。"
    )

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

        # LiteLLM 模型名映射：deepseek-chat -> deepseek/deepseek-chat
        # 若用户已写入前缀则原样使用
        if "/" not in model:
            self._llm_model = f"{provider}/{model}"
        else:
            self._llm_model = model

    # ------------------------------------------------------------------
    # AIProvider 接口
    # ------------------------------------------------------------------
    async def analyze_market(self, market_data: MarketData) -> MarketAnalysisResult:
        """调用 LiteLLM 分析市场状态。"""
        # 延迟导入，避免未安装 litellm 时 import 失败
        from litellm import acompletion  # type: ignore

        user_msg = (
            f"交易对: {market_data.symbol}\n"
            f"当前 K 线: O={market_data.open} H={market_data.high} "
            f"L={market_data.low} C={market_data.close} V={market_data.volume}\n"
            f"1H K 线数量: {len(market_data.ohlcv_1h)}\n"
            f"15m K 线数量: {len(market_data.ohlcv_15m)}\n"
        )

        kwargs: dict[str, Any] = dict(
            model=self._llm_model,
            api_key=self.api_key,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            timeout=6,    # 单个请求 6s 超时（密钥占位 / 离线时尽快走降级，不阻塞主循环）
            max_retries=0,  # 0 次重试（默认 5 次会拉长等待）
        )
        if self.base_url:
            kwargs["api_base"] = self.base_url

        logger.info("AI 分析请求: model={}", self._llm_model)
        try:
            resp = await acompletion(**kwargs)
        except Exception as exc:
            # 网络不可达 / 密钥占位 / LiteLLM 抛错 → 保守降级，不影响上层 API
            logger.warning("LiteLLM 调用失败，降级为 LOW_VOLATILITY 保守判断：{}：{}", type(exc).__name__, exc)
            return MarketAnalysisResult(
                market_regime=MarketRegime.LOW_VOLATILITY,
                confidence=0,
                reason=f"LiteLLM 调用失败: {type(exc).__name__}",
            )
        content = resp.choices[0].message.content or "{}"
        logger.info("AI 分析响应: {}", content)

        try:
            data = json.loads(content)
            return MarketAnalysisResult.model_validate(data)
        except Exception:
            logger.exception("AI 输出解析失败，降级为 LOW_VOLATILITY 保守判断")
            return MarketAnalysisResult(
                market_regime=MarketRegime.LOW_VOLATILITY,
                confidence=0,
                reason=f"解析失败，原始输出：{content[:200]}",
            )
