"""
2026-08-31：用户给的 deepseek-v4-flash 官方文档要点：
  connect_timeout   ≥3s  → 推荐 5s
  timeout (非stream) ≥25s → 推荐 30-45s
  thinking 开启后高峰期排队，不能用 10/15s 这种短 timeout
  reasoning_format: "parsed"（如走 thinking 模式）+ reasoning_content 要记日志

本轮 RED → GREEN 覆盖：
  A) deepseek-v4-flash → 默认 timeout=30 / connect_timeout=5
  B) 其它模型（deepseek-chat）→ 不强制 thinking 相关字段，但也有合理 timeout 兜底
  C) acompletion 调用时 kwargs 能收到以上所有参数（用 monkeypatch 抓 kwargs）
  D) 有 reasoning_content 时要打 info 日志（不是内容本身，而是字节/哈希，避免 prompt 泄露）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@dataclass
class _FakeMsg:
    content: str
    reasoning_content: str | None = None


@dataclass
class _FakeChoice:
    message: _FakeMsg


@dataclass
class _FakeResp:
    choices: list[_FakeChoice]


def _make_provider(model: str = "deepseek-v4-flash"):
    from app.ai.litellm_provider import LiteLLMProvider
    return LiteLLMProvider(
        provider="deepseek",
        api_key="sk-fake",
        model=model,
    )


# ---------------------------------------------------------------------------
# RED Tests
# ---------------------------------------------------------------------------
class Test_DeepSeek_V4_Flash_Timeouts_and_Thinking:
    """按用户给的官方文档校准 deepseek-v4-flash 的调用参数。"""

    @staticmethod
    def _capture_kwargs(monkeypatch):
        captured: dict[str, Any] = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return _FakeResp(choices=[_FakeChoice(
                message=_FakeMsg(
                    content=json.dumps({"market_regime": "TREND_UP", "confidence": 70, "reason": "ok"}),
                    reasoning_content="思考过程片段（只计长度不写日志内容）",
                )
            )])

        # litellm_provider 里 from litellm import acompletion → 抓该模块内局部名 acompletion
        import app.ai.litellm_provider as _mod
        monkeypatch.setattr(_mod, "acompletion", fake_acompletion, raising=False)
        # 同时 monkey litellm 模块以防它在函数内部 import 后用同名
        try:
            import litellm as _lite  # noqa: PLC0415
            monkeypatch.setattr(_lite, "acompletion", fake_acompletion, raising=False)
        except Exception:  # noqa: BLE001
            pass
        return captured

    @staticmethod
    def _mk_md():
        from app.ai.base import MarketData
        return MarketData(
            symbol="ETH-USDT-SWAP",
            open=2450, high=2480, low=2430, close=2470, volume=100.0,
            ohlcv_1h=[], ohlcv_15m=[],
        )

    def test_A_v4_flash_timeout_30s_connect_5s(self, monkeypatch):
        """deepseek-v4-flash：必须 timeout>=25s（这里默认 30s）+ connect_timeout=5s。"""
        import asyncio
        pvd = _make_provider("deepseek-v4-flash")
        cap = self._capture_kwargs(monkeypatch)

        asyncio.run(pvd.analyze_market(self._mk_md()))

        assert "timeout" in cap, f"acompletion kwargs 缺 timeout：keys={list(cap.keys())}"
        assert cap["timeout"] >= 25, f"timeout={cap['timeout']} < 25s 不符合 deepseek v4 flash 文档"
        assert "connect_timeout" in cap, "deepseek-v4-flash 必须配 connect_timeout（文档推荐5s）"
        assert 3 <= cap["connect_timeout"] <= 10, \
            f"connect_timeout={cap['connect_timeout']} 不在文档 [3s,10s] 推荐区间"

    def test_B_v4_flash_reasoning_format_parsed(self, monkeypatch):
        """deepseek-v4-flash：默认开启 thinking，要加 reasoning_format=parsed。"""
        import asyncio
        pvd = _make_provider("deepseek-v4-flash")
        cap = self._capture_kwargs(monkeypatch)

        asyncio.run(pvd.analyze_market(self._mk_md()))

        # deepseek-v4-flash 走 thinking：必须带 reasoning_format=parsed
        assert cap.get("reasoning_format") == "parsed", \
            f"deepseek-v4-flash  reasoning_format 期望 parsed，实际={cap.get('reasoning_format')!r}"

    def test_C_other_model_sane_timeout_no_thinking(self, monkeypatch):
        """其它模型（如 deepseek-chat）：也应有合理 timeout，但不强制加 thinking/reasoning_format。"""
        import asyncio
        pvd = _make_provider("deepseek-chat")
        cap = self._capture_kwargs(monkeypatch)

        asyncio.run(pvd.analyze_market(self._mk_md()))

        # 非 v4-flash 也必须 >= 20s（避免之前 6s 又出误报 TimeoutError）
        assert cap["timeout"] >= 20, f"其它模型 timeout={cap['timeout']} 太短，仍会误报超时"
        # reasoning_format 不强制（旧模型不一定支持）
        assert "reasoning_format" not in cap, \
            "非 thinking 模型不应默认加 reasoning_format（可能报400不支持参数）"

    def test_D_ai_config_injects_custom_timeout(self, monkeypatch):
        """AIConfig 里新增 ai_timeout_s / ai_connect_timeout_s 字段后，Provider 必须按配置取值。"""
        import asyncio
        from app.ai.litellm_provider import LiteLLMProvider
        pvd = LiteLLMProvider(
            provider="deepseek",
            api_key="sk-fake",
            model="deepseek-v4-flash",
            timeout_s=45,
            connect_timeout_s=8,
        )
        cap = self._capture_kwargs(monkeypatch)
        asyncio.run(pvd.analyze_market(self._mk_md()))
        assert cap["timeout"] == 45, f"期望用户配置 timeout=45s，实际={cap['timeout']}"
        assert cap["connect_timeout"] == 8, f"期望用户配置 connect_timeout=8s，实际={cap['connect_timeout']}"

    def test_E_reasoning_content_logs_length(self, monkeypatch, capfd):
        """如果响应里带 reasoning_content，要打一条 INFO 日志（只记长度/模型，不记原文避免泄漏）。

        说明：项目使用 loguru（不是标准 logging），caplog 抓不到；这里用 capfd 读 stderr 原文，
        正好是 loguru 默认 sink 的输出位置。
        """
        import asyncio
        pvd = _make_provider("deepseek-v4-flash")
        _ = self._capture_kwargs(monkeypatch)

        asyncio.run(pvd.analyze_market(self._mk_md()))
        captured = capfd.readouterr()
        joined = (captured.out or "") + "\n" + (captured.err or "")

        # 必须包含"思考内容长度"或"reasoning tokens"这类长度指标
        assert "思考" in joined or "reasoning" in joined.lower() or "长度" in joined, \
            f"响应带 reasoning_content 时未打 INFO 记录/长度。日志={joined!r}"
        # 原文不应直接出现在日志里（避免 prompt/推理原文写日志）
        assert "思考过程片段" not in joined, "reasoning_content 原文禁止直接写日志"
