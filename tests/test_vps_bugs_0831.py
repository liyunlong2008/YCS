"""
2026-08-31 VPS 现场实时抓回 2 个 Bug（curl /api/logs /api/status 验证时发现）：

  Bug A) RuntimeError: cannot reuse already awaited coroutine
      · Trigger: Dashboard 多个 /api/status 请求前后台并发（1 次 get_status_dict 里
        nest_asyncio 跑 _fetch_pos 协程已 await；下一个请求复用同一个本地函数引用时，
        coroutine 对象被当成 cache 但实际是单次消费的）。
      · Log: "Task exception was never retrieved" + coroutine=<Task-1292 _fetch_pos>
             + RuntimeError("cannot reuse already awaited coroutine")

  Bug B) /api/status 顶级别名字段：『最近风控结论』『最近风控原因』『AI节流级别』
         全部显示 "—"。
      · 根因（推）：/api/diag 里写了 last_risk_conclusion / 节流级别 在 system_block 内；
        但 Dashboard JS refresh 里调的是 /api/status，期望顶层返回 key = "最近风控结论"
        而 get_status_dict 里只在 "风控状态.最近一次风控.结论" 里写（嵌套）。
      · 修复：get_status_dict 顶层直接展开这些常用字段，保证 Dashboard curl/json 都能
        直接读出（不需要 drill 3 层 dict）。

TDD：先写 2 个失败用例 → 修 → GREEN。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Bug A: get_status_dict 并发 2 次，不能抛 "cannot reuse already awaited coroutine"
# ---------------------------------------------------------------------------
class Test_A_GetStatusConcurrentPosFetch:
    """get_status_dict() 连续 2 次（模拟 Dashboard AJAX 并发）必须零 unawaited /
    零 RuntimeError 『cannot reuse already awaited coroutine』。"""

    @staticmethod
    def _build_ctl(tmp_path: Path):
        from app.broker.shadow import ShadowBroker
        from app.broker.paper import PaperBroker
        from app.core.config import (
            AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits,
        )
        from app.risk.engine import RiskEngine
        from app.ai.factory import build_ai_provider
        from app.exchange.market import MarketDataProducer
        from app.storage.state_store import StateStore
        from app.storage.trade_journal import TradeJournal
        from app.services.controller import TradingController
        from app.broker.base import MarketSpec

        cfg = AppConfig(
            okx=OKXConfig(api_key="X", secret="X", passphrase="X"),
            ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat"),
            trading=TradingConfig(live=False, default_leverage=10),
            risk_limits=RiskLimits(shadow_mode=False),
        )
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        inner = PaperBroker(symbol=cfg.trading.symbol, initial_balance=1000.0)
        broker = ShadowBroker(inner=inner, symbol=cfg.trading.symbol)
        ai = build_ai_provider(cfg.ai)
        state_store = StateStore(data_dir)
        journal = TradeJournal(data_dir)
        risk = RiskEngine()
        mp = MarketDataProducer(okx=cfg.okx, symbol=cfg.trading.symbol)
        ctl = TradingController(
            config=cfg, broker=broker, ai=ai, risk=risk,
            state_store=state_store, journal=journal,
            market_producer=mp,
        )
        # 挂一个最小 market_spec（RiskEngine 用）
        try:
            spec = MarketSpec(symbol=cfg.trading.symbol)
            ctl.market_spec = spec  # type: ignore[attr-defined]
        except Exception:
            pass
        return ctl

    def test_A_2_calls_no_reuse_error(self, tmp_path):
        """连续 2 次 get_status_dict()：旧实现在 running loop 分支里 nest_asyncio 跑
        _coro2 → 偶发 "RuntimeError: cannot reuse already awaited coroutine" +
        "Task exception was never retrieved"（现场 Task-1292: _fetch_pos）。

        最接近 VPS 现场的触发方式：把调用放进 ASYNC 上下文（制造 running loop），
        然后并行 2 次。修完应 0 异常。
        """
        import asyncio as _aio
        ctl = self._build_ctl(tmp_path)

        async def _runner():
            r1 = _aio.to_thread(ctl.get_status_dict)
            r2 = _aio.to_thread(ctl.get_status_dict)
            d1, d2 = await _aio.gather(r1, r2)
            return d1, d2

        import warnings
        with warnings.catch_warnings(record=True) as wl:
            warnings.simplefilter("always")
            d1, d2 = _aio.run(_runner())

        assert "当前持仓" in d1 and "当前持仓" in d2
        # 关键断言：没有 RuntimeError reuse 消息 / 没有 unawaited
        reuse_warns = [str(w.message) for w in wl if "already awaited" in str(w.message) or "never awaited" in str(w.message)]
        assert reuse_warns == [], f"发现未回收协程警告: {reuse_warns}"

    def test_B_top_level_risk_throttle_fields(self, tmp_path):
        """/api/status 顶层要『最近风控结论』『最近风控原因』『AI节流级别』。
        Dashboard JS refresh 直接读顶层键；如果嵌套才会读到 "—"。"""
        ctl = self._build_ctl(tmp_path)
        # 手动塞一个 last_verdict（模拟 bg_main_loop 已跑过一轮风控）
        from app.risk.engine import RiskVerdict
        ctl.risk.last_verdict = RiskVerdict(
            allow=False, passed=False,
            reason="余额 14.8U 小，按 5X 杠杆下，建议名义 2.45U < 最小 2.47U（缺 0.02U≈1X）",
            suggested_notional_usdt=2.45,
            effective_min_notional_usdt=2.47,
            suggested_leverage=5,
        )
        ctl.risk.last_verdict_at = int(__import__("time").time())
        # 塞一个节流状态：AIThrottler.to_status_dict 读的是 state.level（不是实例 _level），
        #   且 get_status_dict 内部会调 should_call_ai() 再把 state.level 写一遍（IDLE 默认），
        #   所以这里显式把 state.level 改成 NORMAL + patch should_call_ai 让它不再覆盖。
        from app.core.ai_throttle import AIThrottler, ThrottleLevel
        thr = AIThrottler()
        thr.state.level = ThrottleLevel.NORMAL.value
        thr._orig_should = thr.should_call_ai
        def _no_side_effect_should(*a, **kw):
            # 返回一个假决策（True=允许调 AI），但不写 self.state.level，不覆盖预设值。
            from app.core.ai_throttle import ThrottleDecision
            return ThrottleDecision(
                should=True, level=ThrottleLevel.NORMAL,
                reason="", wait_s=0, base_interval_s=60, dyn_mult=1.0,
            )
        thr.should_call_ai = _no_side_effect_should  # type: ignore[method-assign]
        ctl.ai_throttler = thr  # type: ignore[attr-defined]

        d = ctl.get_status_dict()

        # 顶级别名（Dashboard 直接用）
        assert "最近风控结论" in d, f"顶层缺少『最近风控结论』，keys[top30]={list(d.keys())[:30]}"
        assert "最近风控原因" in d, "顶层缺少『最近风控原因』"
        assert "AI节流级别" in d, "顶层缺少『AI节流级别』"
        assert d["最近风控结论"] == "拒绝", f"期望拒绝，实际={d['最近风控结论']}"
        assert "2.45U" in d["最近风控原因"] or "建议名义" in d["最近风控原因"] or "缺" in d["最近风控原因"], \
            f"顶层『最近风控原因』无风控细节：{d['最近风控原因']!r}"
        assert d["AI节流级别"] == "NORMAL", f"期望 AI节流级别=NORMAL，实际={d['AI节流级别']}"
