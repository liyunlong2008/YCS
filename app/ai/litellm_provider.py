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

    # 2026-08-31 deepseek-v4-flash 官方文档默认值（用户给的参考表）：
    #   connect_timeout  5s    业务最小 3s / 推荐 5s
    #   timeout(非stream) 30s  业务最小 25s / 生产推荐 30-45s
    #   thinking 模式：v4-flash 默认开启，高峰期也需 30s 才能出 reasoning+answer
    _DEFAULT_CONNECT_TIMEOUT_S: int = 5
    _DEFAULT_TIMEOUT_S: int = 30
    _DEFAULT_NON_FLASH_TIMEOUT_S: int = 20  # 非 flash 模型：取中间档（避免 6s 误报，也不拖到 45s）

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
        *,
        connect_timeout_s: int = 0,
        timeout_s: int = 0,
        thinking_enabled: bool | None = None,
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

        # 是否 deepseek v4 flash：模型名（不含 provider 前缀）包含 "v4-flash" 就判是
        _pure_model = model.split("/")[-1].lower()
        self._is_v4_flash: bool = ("v4-flash" in _pure_model) or ("flash" == _pure_model)

        # thinking：None=自动（v4-flash 默认开）；True/False=用户强控
        self.thinking_enabled: bool
        if thinking_enabled is None:
            self.thinking_enabled = self._is_v4_flash
        else:
            self.thinking_enabled = bool(thinking_enabled)

        # connect_timeout：显式传入>0 用显式，否则 5s
        self.connect_timeout_s: int = (
            int(connect_timeout_s) if int(connect_timeout_s) > 0 else self._DEFAULT_CONNECT_TIMEOUT_S
        )
        # timeout：显式传入>0 用显式；否则 v4-flash=30s / 其它=20s
        if int(timeout_s) > 0:
            self.timeout_s: int = int(timeout_s)
        elif self._is_v4_flash:
            self.timeout_s = self._DEFAULT_TIMEOUT_S
        else:
            self.timeout_s = self._DEFAULT_NON_FLASH_TIMEOUT_S

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
            # 2026-08-31：按 deepseek-v4-flash 官方文档校准
            timeout=self.timeout_s,
            connect_timeout=self.connect_timeout_s,
            max_retries=0,  # 0 次重试（重试会与 bg_main_loop 的 10s 心跳叠加→连续卡）
        )
        if self.base_url:
            kwargs["api_base"] = self.base_url

        # thinking 模式：仅在开启时传 reasoning_format=parsed（支持 reasoning_content 的参数）
        if self.thinking_enabled:
            kwargs["reasoning_format"] = "parsed"
            # LiteLLM/官方 SDK 推荐：显式声明希望拿到 reasoning 原文（若被默认关掉）
            # 注：deepseek/v4-flash 原生支持 reasoning_content；为兼容其它 SDK 不同参数名，这里用"多参数兼容写入"
            #   - LiteLLM 通过 extra_body 下传 "thinking" 字段，官方 SDK 也要求显式 True
            kwargs["extra_body"] = dict(kwargs.get("extra_body") or {})
            kwargs["extra_body"].setdefault("thinking", True)

        logger.info(
            "AI 分析请求: model={} thinking={} connect_timeout={}s timeout={}s",
            self._llm_model, self.thinking_enabled, self.connect_timeout_s, self.timeout_s,
        )
        try:
            resp = await acompletion(**kwargs)
        except Exception as exc:
            # 网络不可达 / 密钥占位 / LiteLLM 抛错 → 保守降级，不影响上层 API
            logger.warning(
                "LiteLLM 调用失败（connect={}s total={}s），降级为 LOW_VOLATILITY 保守判断：{}：{}",
                self.connect_timeout_s, self.timeout_s,
                type(exc).__name__, exc,
            )
            return MarketAnalysisResult(
                market_regime=MarketRegime.LOW_VOLATILITY,
                confidence=0,
                reason=f"LiteLLM 调用失败: {type(exc).__name__}",
            )

        # 取 message：支持 LiteLLM 的标准结构 + reasoning_content 字段
        try:
            _msg = resp.choices[0].message
            content = getattr(_msg, "content", None) or "{}"
            reasoning: str | None = getattr(_msg, "reasoning_content", None)
            if reasoning and isinstance(reasoning, str) and reasoning.strip():
                reasoning_len = len(reasoning)
                reasoning_tokens_est = max(1, reasoning_len // 4)  # 粗估 4 字符≈1 token
                logger.info(
                    "AI 思考内容: 长度≈{}chars(约{}tokens) 模型={}（原文不写入日志以保护隐私）",
                    reasoning_len, reasoning_tokens_est, self._llm_model,
                )
        except Exception:  # noqa: BLE001
            content = "{}"
            reasoning = None

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
