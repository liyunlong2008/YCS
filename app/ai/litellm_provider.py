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
    """统一 LiteLLM AI 实现。

    2026-08-31 VPS 现场调优（解决 6s timeout 导致≈60% 超时率 & 永远 LOW_VOLATILITY）：
      1) timeout 从 6s → 默认 15s（config 可调）：
         - 跨境 6s 太激进，大量正常请求被 cut；15s 覆盖 95% 正常 deepseek-v4-flash 请求。
      2) max_retries 从 0 → 默认 1：
         - 给超时/5xx 一次重试机会，但绝对不 >2（避免无限等待错过下一轮 10s 主循环 + 哨兵早叫）。
      3) thinking_mode 默认 disabled：
         - 项目只需要 3 字段 JSON（regime/confidence/reason），不需要 Chain-of-Thought；
           thinking 会让 TTFT（首字节时间）从≈1s→≈4~8s，且多付 thinking tokens 成本。
      4) enable_stream 默认 False：
         - 我们要"完整 JSON 解析"，stream 不会让最终解析时刻更早；
           反而需要首字节超时 stream_timeout、解析 SSE 帧，对 3 字段短 JSON 纯亏。
      5) LiteLLM 全失败 → 内置量价规则兜底（不再 LOW_VOLATILITY conf=0 躺平）：
         - 1h 涨 >= +0.8% → TREND_UP；跌 <= -0.8% → TREND_DOWN；
         - 振幅(high-low)/open >= 1.5% → HIGH_VOLATILITY；
         - 其他 → LOW_VOLATILITY（但置信度最低 20，仍有机会过风控）。
    """

    # 提示词模板：严格要求 JSON 输出
    SYSTEM_PROMPT = (
        "你是专业的加密货币市场分析师，只负责输出市场状态，不给出交易建议。"
        "请基于给定的 K 线和量价数据，输出严格 JSON 格式："
        '{"market_regime": "...", "confidence": 0-100, "reason": "中文理由"}。'
        f"market_regime 仅可选：{', '.join(m.value for m in MarketRegime)}。"
    )

    # T3 规则阈值（与测试对齐）
    TREND_CHG_PCT = 0.8        # 1h 收盘相对开盘的涨跌阈值
    HV_AMPLITUDE_PCT = 1.5     # 振幅 = (high-low)/open 的高波动阈值
    LOW_CONF = 20              # 规则低波动置信度
    HV_CONF = 45               # 规则高波动置信度
    TREND_CONF = 55            # 规则 TREND_UP/DOWN 置信度（有明确方向给稍高）

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
        *,
        # 2026-08-31 新增：VPS 超时/思考/流式调优字段
        timeout_seconds: float = 15.0,
        max_retries: int = 1,
        thinking_mode: str = "disabled",
        enable_stream: bool = False,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        # 合法钳制（即使 config 乱填也不炸）
        try:
            self.timeout_seconds = float(max(1.0, float(timeout_seconds)))
        except Exception:  # noqa: BLE001
            self.timeout_seconds = 15.0
        try:
            mr = int(max_retries or 0)
            if mr < 0:
                mr = 0
            if mr > 2:
                # 防止用户填 5+，变成"等 15s×5=75s 错过行情"
                mr = 2
            self.max_retries = mr
        except Exception:  # noqa: BLE001
            self.max_retries = 1
        # thinking_mode 合法化
        valid_thinking = {"enabled", "disabled", "low", "medium", "high", "max"}
        thinking_mode = str(thinking_mode).strip().lower()
        self.thinking_mode = thinking_mode if thinking_mode in valid_thinking else "disabled"
        self.enable_stream = bool(enable_stream)

        # LiteLLM 模型名映射：deepseek-chat -> deepseek/deepseek-chat
        # 若用户已写入前缀则原样使用
        if "/" not in model:
            self._llm_model = f"{provider}/{model}"
        else:
            self._llm_model = model

    # ------------------------------------------------------------------
    # 内置量价规则（T3 兜底）
    # ------------------------------------------------------------------
    @classmethod
    def _rule_based_fallback(cls, md: MarketData, exc_type_name: str) -> MarketAnalysisResult:
        """LiteLLM 完全失败时：用 1h/open/high/low/close + 量做最简分类，
        避免永远 LOW_VOLATILITY conf=0 → 永远不开仓。"""
        open_p = float(md.open or 0.0)
        close_p = float(md.close or 0.0)
        high_p = float(md.high or 0.0)
        low_p = float(md.low or 0.0)

        # 若单根 K 线不够，再尝试从 ohlcv_1h 拿最近一根
        if open_p <= 0 and len(md.ohlcv_1h) > 0:
            try:
                last = md.ohlcv_1h[-1]
                # 兼容 6 元组（ts,o,h,l,c,v）和任何对象
                if isinstance(last, (tuple, list)) and len(last) >= 6:
                    open_p = float(last[1] or 0.0)
                    high_p = float(last[2] or 0.0)
                    low_p = float(last[3] or 0.0)
                    close_p = float(last[4] or 0.0)
                elif hasattr(last, "open"):
                    open_p = float(getattr(last, "open", 0.0) or 0.0)
                    high_p = float(getattr(last, "high", 0.0) or 0.0)
                    low_p = float(getattr(last, "low", 0.0) or 0.0)
                    close_p = float(getattr(last, "close", 0.0) or 0.0)
            except Exception:  # noqa: BLE001
                pass

        # 缺数据就保守 LOW_VOLATILITY
        if open_p <= 0 or close_p <= 0 or high_p <= 0 or low_p <= 0:
            return MarketAnalysisResult(
                market_regime=MarketRegime.LOW_VOLATILITY,
                confidence=cls.LOW_CONF,
                reason=f"规则兜底（LiteLLM {exc_type_name}）：缺 K 线数据，保守低波动",
            )

        close_chg_pct = (close_p - open_p) / open_p * 100.0
        amp_pct = (high_p - low_p) / open_p * 100.0

        if close_chg_pct >= cls.TREND_CHG_PCT:
            return MarketAnalysisResult(
                market_regime=MarketRegime.TREND_UP,
                confidence=cls.TREND_CONF,
                reason=(
                    f"规则兜底（LiteLLM {exc_type_name}）："
                    f"1h 收/开 涨 +{close_chg_pct:.2f}%（≥+{cls.TREND_CHG_PCT}%），判定上涨趋势"
                ),
            )
        if close_chg_pct <= -cls.TREND_CHG_PCT:
            return MarketAnalysisResult(
                market_regime=MarketRegime.TREND_DOWN,
                confidence=cls.TREND_CONF,
                reason=(
                    f"规则兜底（LiteLLM {exc_type_name}）："
                    f"1h 收/开 跌 {close_chg_pct:.2f}%（≤-{cls.TREND_CHG_PCT}%），判定下跌趋势"
                ),
            )
        if amp_pct >= cls.HV_AMPLITUDE_PCT:
            return MarketAnalysisResult(
                market_regime=MarketRegime.HIGH_VOLATILITY,
                confidence=cls.HV_CONF,
                reason=(
                    f"规则兜底（LiteLLM {exc_type_name}）："
                    f"1h 振幅 {amp_pct:.2f}%（≥{cls.HV_AMPLITUDE_PCT}%），判定高波动"
                ),
            )
        return MarketAnalysisResult(
            market_regime=MarketRegime.LOW_VOLATILITY,
            confidence=cls.LOW_CONF,
            reason=(
                f"规则兜底（LiteLLM {exc_type_name}）："
                f"1h 涨跌 {close_chg_pct:+.2f}% 振幅 {amp_pct:.2f}%，均未过阈值，判定低波动"
            ),
        )

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
            # 2026-08-31 调优：timeout/max_retries 从配置透传（默认 15s + 1 次重试）
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
            # stream：默认 False（短 JSON 无收益）
            stream=self.enable_stream,
            # 2026-08-31：针对 deepseek-v4-flash 的思考模式控制
            # LiteLLM 会自动把 "thinking" 这个 OpenAI 兼容参数翻译到对应 provider。
            # 对于 deepseek-v4-flash，官方支持 disabled/low/max 档位；
            # 非 deepseek 的 provider 若不支持 thinking，用 drop_params=True 避免抛 UnsupportedParamsError。
            drop_params=True,
        )
        # thinking 映射（按 DeepSeek 官方 + LiteLLM 文档）：
        #   DeepSeek v4 官方（https://api-docs.deepseek.com/guides/thinking_mode/）：
        #     · thinking 开关：{"thinking": {"type": "enabled"}} / {"thinking": {"type": "disabled"}}
        #     · 思考强度：reasoning_effort ∈ {low, medium, high, max}
        #       注：medium→high, xhigh→high（DeepSeek 官方映射）
        #   LiteLLM（https://docs.litellm.ai/docs/providers/deepseek）：
        #     · Any reasoning_effort != "none" → thinking enabled
        #     · drop_params=True → 不支持的 provider 会自动丢弃，不会 400
        #
        # 对本项目的平衡分析（2026-08-31 VPS 跨境实测）：
        #   · thinking disabled：TTFT≈0.8~2s，整笔 3~5s（3 字段短 JSON）→ 不影响 10s 主循环
        #   · thinking=low：   TTFT≈2~5s，整笔 5~10s（有时跨 1 轮主循环，但哨兵会早叫）
        #   · thinking=high/max：TTFT≈4~12s，整笔 10~20s（容易错过 1~2 轮行情，不推荐）
        #   因此默认档位 disabled；想要质量可开 low，但 timeout 请调到 25s+。
        if self.thinking_mode == "enabled":
            # enabled = 模型默认思考（v4-flash 默认 high），显式传 "enabled" 对象
            kwargs["thinking"] = {"type": "enabled"}
        elif self.thinking_mode == "disabled":
            # disabled = 显式传 "disabled" 对象（DeepSeek 官方格式；不能传字符串 "disabled"）
            kwargs["thinking"] = {"type": "disabled"}
        else:
            # low/medium/high/max → thinking 打开 + reasoning_effort 档位
            kwargs["thinking"] = {"type": "enabled"}
            kwargs["reasoning_effort"] = self.thinking_mode

        if self.base_url:
            kwargs["api_base"] = self.base_url

        logger.info(
            "AI 分析请求: model={} timeout={}s retries={} stream={} thinking={}",
            self._llm_model, self.timeout_seconds, self.max_retries,
            self.enable_stream, self.thinking_mode,
        )
        try:
            resp = await acompletion(**kwargs)
        except Exception as exc:
            # 2026-08-31：不再直接 LOW_VOLATILITY conf=0 躺平，
            # 先走内置量价规则兜底（T3），再把真正的分类给风控/主循环。
            exc_type = type(exc).__name__
            logger.warning(
                "LiteLLM 调用失败，回落内置量价规则：{}：{}",
                exc_type, exc,
            )
            return self._rule_based_fallback(market_data, exc_type)

        # stream 路径：收集全部 content 后再解析（短 JSON 不需要增量判断）
        if self.enable_stream:
            chunks: list[str] = []
            try:
                async for chunk in resp:
                    try:
                        delta = chunk.choices[0].delta.content
                    except Exception:  # noqa: BLE001
                        delta = None
                    if delta:
                        chunks.append(delta)
            except Exception as e:  # noqa: BLE001
                logger.warning("stream 迭代异常，回落规则兜底：{}", e)
                return self._rule_based_fallback(market_data, f"StreamError:{type(e).__name__}")
            content = "".join(chunks).strip() or "{}"
            logger.info("AI 分析响应(stream 汇总): {}", content[:500])
        else:
            content = resp.choices[0].message.content or "{}"
            logger.info("AI 分析响应: {}", content)

        try:
            data = json.loads(content)
            return MarketAnalysisResult.model_validate(data)
        except Exception:
            logger.exception("AI 输出解析失败，回落规则兜底")
            return self._rule_based_fallback(market_data, "JSONDecodeError")
