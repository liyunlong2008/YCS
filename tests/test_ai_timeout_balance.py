"""
2026-08-31：平衡「AI 响应时间 vs 不丢行情」专项测试（TDD RED 先红）。

目标：
  T1. AIConfig 新增 timeout_seconds / max_retries / thinking_mode / enable_stream 四个可配字段。
  T2. LiteLLMProvider 把上述 4 参数正确透传给 acompletion（kwargs 里能拿到正确值）。
      · thinking=disabled 对应 deepseek-v4-flash 官方「非思考模式」更省 tokens 更快首字节
      · enable_stream 默认 False（本项目只要严格 JSON，不需要 SSE 增量读）
      · timeout 默认 15s（之前 6s 过短，VPS 跨境链路 60% 超时率）
      · max_retries 默认 1（超时/5xx 仅 1 次有限重试，不无限重试错过行情）
  T3. LiteLLM 全部失败（Timeout 连续 2 次）时：不再永远回落 LOW_VOLATILITY，
      改为「内置量价规则兜底」：
        · 1h 收盘相对开盘 涨 >= +0.8% → TREND_UP
        · 跌 <= -0.8% → TREND_DOWN
        · 否则 → HIGH_VOLATILITY（若高低价差 >= 1.5%）或 LOW_VOLATILITY
      这样就算 DeepSeek 全挂，主循环也能按规则过风控，不会"躺平不开仓"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# T1. AIConfig 新字段默认值 & 类型
# ---------------------------------------------------------------------------
class Test_T1_AIConfig_New_Fields_Defaults:
    @staticmethod
    def test_default_values_and_types():
        from app.core.config import AIConfig
        cfg = AIConfig(provider="deepseek", api_key="X", model="deepseek-v4-flash")
        # 默认值：timeout=15, max_retries=1, thinking=disabled, stream=False
        assert cfg.timeout_seconds == 15, f"默认 timeout 期望 15，实际 {cfg.timeout_seconds}"
        assert cfg.max_retries == 1, f"默认 max_retries 期望 1，实际 {cfg.max_retries}"
        assert cfg.thinking_mode == "disabled", f"默认 thinking 期望 disabled，实际 {cfg.thinking_mode!r}"
        assert cfg.enable_stream is False, f"默认 enable_stream 期望 False，实际 {cfg.enable_stream}"
        # 类型 & 合法范围检查
        assert isinstance(cfg.timeout_seconds, (int, float)) and cfg.timeout_seconds > 0
        assert isinstance(cfg.max_retries, int) and cfg.max_retries >= 0
        assert cfg.thinking_mode in {"enabled", "disabled", "low", "medium", "high", "max"}


# ---------------------------------------------------------------------------
# T2. LiteLLMProvider 构造 kwargs 是否正确透传
# ---------------------------------------------------------------------------
class Test_T2_LiteLLM_Kwargs_Passthrough:
    @staticmethod
    def _patched_provider(tmp_path: Path, **ai_kwargs: Any):
        """构造一个 LiteLLMProvider；monkey-patch acompletion 捕获它被调用时的 kwargs。"""
        from app.ai.litellm_provider import LiteLLMProvider

        captured: dict[str, Any] = {}

        async def fake_acompletion(**kw):
            captured.clear()
            captured.update(kw)
            # 返回一个能被 content 读取的假响应
            class _M:
                content = '{"market_regime": "TREND_UP", "confidence": 80, "reason": "测试"}'
            class _C:
                message = _M()
            class _R:
                choices = [_C()]
            return _R()

        import app.ai.litellm_provider as mod_pkg
        old = getattr(mod_pkg, "_acompletion_injected", None)
        # monkeypatch inside the method: acompletion symbol imported from litellm inside analyze_market local scope
        cfg_kw: dict[str, Any] = dict(
            provider="deepseek",
            api_key="X",
            model="deepseek/deepseek-v4-flash",
        )
        cfg_kw.update(ai_kwargs)
        prov = LiteLLMProvider(**cfg_kw)
        return prov, captured, fake_acompletion

    @pytest.mark.asyncio
    async def test_non_stream_defaults_passthrough(self, tmp_path: Path):
        """默认配置下：timeout 15s / max_retries 1 / stream=False / thinking disabled。"""
        from app.ai.base import MarketData
        prov, captured, fake = self._patched_provider(tmp_path)

        # Patch: 直接把 Provider 内部的 acompletion 替换为 fake
        # 由于 LiteLLMProvider 在 analyze_market 内部 from litellm import acompletion 动态引入，
        # 我们通过 monkeypatch sys.modules["litellm"].acompletion 做拦截。
        import sys
        litellm_mod = type(sys)("litellm")
        litellm_mod.acompletion = fake  # type: ignore[attr-defined]
        sys.modules["litellm"] = litellm_mod  # type: ignore[assignment]

        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2500, high=2520, low=2480, close=2505, volume=1_000_000,
            ohlcv_1h=[], ohlcv_15m=[],
        )
        await prov.analyze_market(md)

        assert captured.get("timeout") == 15, f"timeout 透传错，captured.timeout={captured.get('timeout')}"
        assert captured.get("max_retries") == 1, f"max_retries 透传错，实际={captured.get('max_retries')}"
        assert captured.get("stream") is False, f"stream 默认应为 False，实际={captured.get('stream')}"
        # deepseek-v4-flash：thinking disabled → 参数名按 deepseek 官方规范 reasoning_effort / thinking 二选一；
        # 我们用 LiteLLM 会自动翻译为 "thinking":"disabled"；这里只检查 extra 里有 thinking/disabled 或 reasoning_effort。
        extra_str = str(captured).lower()
        assert ("thinking" in extra_str and "disabled" in extra_str) or "reasoning_effort" in extra_str, \
            f"kwargs 里没有透传思考模式关闭！完整 captured keys top: {list(captured.keys())[:15]}"

    @pytest.mark.asyncio
    async def test_custom_timeout_retry_stream_thinking(self, tmp_path: Path):
        """自定义配置覆盖默认值：timeout=25 / max_retries=0 / enable_stream=True / thinking='low'"""
        from app.ai.base import MarketData
        prov, captured, fake = self._patched_provider(
            tmp_path,
            timeout_seconds=25,
            max_retries=0,
            enable_stream=True,
            thinking_mode="low",
        )
        import sys
        litellm_mod = type(sys)("litellm")
        litellm_mod.acompletion = fake  # type: ignore[attr-defined]
        sys.modules["litellm"] = litellm_mod  # type: ignore[assignment]

        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2500, high=2520, low=2480, close=2505, volume=1_000_000,
            ohlcv_1h=[], ohlcv_15m=[],
        )
        await prov.analyze_market(md)
        assert captured.get("timeout") == 25
        assert captured.get("max_retries") == 0
        assert captured.get("stream") is True
        extra_low = str(captured).lower()
        assert "low" in extra_low, (
            f"自定义 thinking_mode=low 未传进 kwargs。captured keys[:10]={list(captured.keys())[:10]}"
        )


# ---------------------------------------------------------------------------
# T3. LiteLLM 全失败时：内置量价规则兜底，不能永远 LOW_VOLATILITY
# ---------------------------------------------------------------------------
class Test_T3_RuleBased_Fallback_When_LiteLLM_Fails:
    @staticmethod
    def _make_provider_that_always_raises(tmp_path: Path):
        from app.ai.litellm_provider import LiteLLMProvider

        async def always_fail(**kw):
            raise TimeoutError("manually injected timeout for test T3 rule-fallback")

        import sys
        litellm_mod = type(sys)("litellm")
        litellm_mod.acompletion = always_fail  # type: ignore[attr-defined]
        sys.modules["litellm"] = litellm_mod  # type: ignore[assignment]

        prov = LiteLLMProvider(provider="deepseek", api_key="X", model="deepseek-v4-flash")
        return prov

    @pytest.mark.asyncio
    async def test_t3_up_trend_recover(self, tmp_path):
        """1h 收-开=+1.5% (>= 0.8%) → 必须 TREND_UP（不回 LOW_VOLATILITY conf=0）。"""
        from app.core.constants import MarketRegime
        from app.ai.base import MarketData
        prov = self._make_provider_that_always_raises(tmp_path)
        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2400, high=2440, low=2395, close=2436,  # close/open=1.015 → +1.5%
            volume=5_000_000,
            ohlcv_1h=[(0, 2400, 2440, 2395, 2436, 5_000_000)],
            ohlcv_15m=[],
        )
        res = await prov.analyze_market(md)
        assert res.market_regime == MarketRegime.TREND_UP, (
            f"1h +1.5% 场景：期望 TREND_UP，实际={res.market_regime} reason={res.reason}"
        )
        assert res.confidence >= 40, f"规则兜底置信度最低 40，实际={res.confidence}"

    @pytest.mark.asyncio
    async def test_t3_down_trend_recover(self, tmp_path):
        """1h 收-开=-1.5%（<= -0.8%）→ TREND_DOWN。"""
        from app.core.constants import MarketRegime
        from app.ai.base import MarketData
        prov = self._make_provider_that_always_raises(tmp_path)
        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2500, high=2510, low=2460, close=2462,  # -1.52%
            volume=5_000_000,
            ohlcv_1h=[(0, 2500, 2510, 2460, 2462, 5_000_000)],
            ohlcv_15m=[],
        )
        res = await prov.analyze_market(md)
        assert res.market_regime == MarketRegime.TREND_DOWN, (
            f"1h -1.52% 场景：期望 TREND_DOWN，实际={res.market_regime} reason={res.reason}"
        )
        assert res.confidence >= 40

    @pytest.mark.asyncio
    async def test_t3_high_vol_whipsaw(self, tmp_path):
        """横盘但振幅>=2%（high/low 差 >= 2%）→ HIGH_VOLATILITY。"""
        from app.core.constants import MarketRegime
        from app.ai.base import MarketData
        prov = self._make_provider_that_always_raises(tmp_path)
        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2500, high=2530, low=2479, close=2502,  # 差 2.04%
            volume=4_000_000,
            ohlcv_1h=[(0, 2500, 2530, 2479, 2502, 4_000_000)],
            ohlcv_15m=[],
        )
        res = await prov.analyze_market(md)
        assert res.market_regime == MarketRegime.HIGH_VOLATILITY, (
            f"振幅 2.04% 场景：期望 HIGH_VOLATILITY，实际={res.market_regime} reason={res.reason}"
        )

    @pytest.mark.asyncio
    async def test_t3_low_vol_sideways(self, tmp_path):
        """振幅<1.5% + 涨跌<0.8% → 才回到 LOW_VOLATILITY（但置信度给最低 20 不 0，避免风控总不过）。"""
        from app.core.constants import MarketRegime
        from app.ai.base import MarketData
        prov = self._make_provider_that_always_raises(tmp_path)
        md = MarketData(
            symbol="ETH-USDT-SWAP",
            open=2500, high=2515, low=2490, close=2505,  # 幅 1% / 涨跌 0.2%
            volume=2_000_000,
            ohlcv_1h=[(0, 2500, 2515, 2490, 2505, 2_000_000)],
            ohlcv_15m=[],
        )
        res = await prov.analyze_market(md)
        assert res.market_regime == MarketRegime.LOW_VOLATILITY
        # 规则兜底给出置信度=20，不会再是 conf=0
        assert 10 <= res.confidence <= 30, (
            f"规则低波动置信度期望 10~30 区间，实际={res.confidence}"
        )
