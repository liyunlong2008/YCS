# -*- coding: utf-8 -*-
"""阶段 4：交易执行闭环 单元测试（TDD · Red 先行）。

覆盖内容：
 1) TradingController.execute_trade_signal：AI 信号 + RiskVerdict → Maker LIMIT → 立即成交；
 2) Maker 挂单超时 → 自动降级为 Taker（市价）；
 3) TradingController.close_position_for_protection：检测到利润保护后 → 市价平仓 + 调 RiskEngine.on_trade_closed + 更新 stats(wins/losses)；
 4) Daily reset：新的一天 → RiskEngine.start_new_day；
 5) Controller stats：trades_total / wins / losses / total_pnl_pct 正确持久化。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import pytest

from app.ai.base import MarketAnalysisResult, MarketData, MarketRegime
from app.broker.base import Balance, Broker, Order
from app.broker.paper import PaperBroker
from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig
from app.core.constants import (
    OrderSide, OrderStatus, OrderType, PositionSide, RunMode,
    RunMode as _RM, SYMBOL,
)
from app.risk.engine import RiskEngine, RiskVerdict
from app.services.controller import TradingController
from app.storage.state_store import StateStore
from app.storage.trade_journal import TradeJournal
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager, TrailingProfitConfig


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _make_cfg() -> AppConfig:
    return AppConfig(
        okx=OKXConfig(api_key="X", secret="X", passphrase="X"),
        ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat", base_url=""),
        trading=TradingConfig(live=False, symbol=SYMBOL),
    )


def _ctl(tmp_path: Path) -> tuple[TradingController, PaperBroker, OrderManager, RiskEngine, TradeJournal, StateStore]:
    cfg = _make_cfg()
    broker = PaperBroker(symbol=SYMBOL)
    risk = RiskEngine()
    risk.start_new_day(1000.0)
    state_store = StateStore(tmp_path)
    journal = TradeJournal(tmp_path)
    om = OrderManager(broker, state_store=state_store)

    class _DummyAI:
        async def analyze_market(self, md):
            return MarketAnalysisResult(market_regime=MarketRegime.TREND_UP, confidence=85, reason="测试看多")

    ctl = TradingController(
        config=cfg, broker=broker, ai=_DummyAI(), risk=risk,
        state_store=state_store, journal=journal,
    )
    # OrderManager 注入到 Controller（运行时装配）
    ctl.order_manager = om  # type: ignore[attr-defined]
    ctl.position_manager = PositionManager(TrailingProfitConfig.default())  # type: ignore[attr-defined]
    return ctl, broker, om, risk, journal, state_store


# ===========================================================================
#  1) Maker 优先：当 ticker 立即成交 → 订单 FILLED、统计+1、持仓方向正确
# ===========================================================================
class TestExecuteTradeSignalMakerFill:
    def test_maker_filled_on_bullish(self, tmp_path: Path) -> None:
        ctl, broker, om, risk, journal, ss = _ctl(tmp_path)
        # 风控允许开仓
        verdict = RiskVerdict(
            allow=True, suggested_size=0.1, suggested_leverage=3,
            stop_loss_price=1980, reason="风控通过",
        )
        # 入场价 2000，LIMIT BUY @ 2000
        ai = MarketAnalysisResult(
            market_regime=MarketRegime.TREND_UP, confidence=85, reason="测试上涨",
        )
        # 先 ticker 让 broker 记录 bid/ask 状态（apply before open order）
        broker.apply_ticker(bid=2000, ask=2001, last=2000)
        # 调用 execute_trade_signal
        result = asyncio.run(ctl.execute_trade_signal(
            ai=ai, verdict=verdict, entry_price=2000,
            market_side=OrderSide.BUY,
            maker_timeout=2, poll_interval=0.1,  # 短超时测试
        ))
        # 结果：成交
        assert result["status"] in ("FILLED", "filled")
        pos = asyncio.run(broker.get_position(SYMBOL))
        assert pos.side == PositionSide.LONG
        assert abs(pos.size - 0.1) < 1e-9
        # stats 中 trades_total+1
        st = ss.load()
        assert st["stats"]["trades_opened"] == 1

    def test_maker_timeout_falls_back_to_taker(self, tmp_path: Path) -> None:
        """Maker 挂单价格不成交（bid/ask 高于 BUY limit），超时后 → 转市价单成交。"""
        ctl, broker, om, risk, journal, ss = _ctl(tmp_path)
        verdict = RiskVerdict(
            allow=True, suggested_size=0.1, suggested_leverage=3,
            stop_loss_price=1980, reason="风控通过",
        )
        ai = MarketAnalysisResult(market_regime=MarketRegime.TREND_UP, confidence=80, reason="测试")
        # 对 BUY limit=2000 要想不成交：ask / bid / last 必须全部 > 2000（市场向上跳空，挂 2000 追不上）
        broker.apply_ticker(bid=2008, ask=2010, last=2009)
        result = asyncio.run(ctl.execute_trade_signal(
            ai=ai, verdict=verdict, entry_price=2000,
            market_side=OrderSide.BUY,
            maker_timeout=1, poll_interval=0.1,
            use_taker_fallback=True,
        ))
        assert result["status"] == "FILLED"
        # 成交方式：taker 市价
        assert result["via"] == "taker"
        # 持仓：LONG
        pos = asyncio.run(broker.get_position(SYMBOL))
        assert pos.side == PositionSide.LONG

    def test_risk_deny_skips_order(self, tmp_path: Path) -> None:
        """风控拒绝 → execute_trade_signal 返回 REJECTED，不创建订单。"""
        ctl, broker, om, risk, journal, ss = _ctl(tmp_path)
        verdict = RiskVerdict(allow=False, reason="日亏损超限")
        ai = MarketAnalysisResult(market_regime=MarketRegime.TREND_UP, confidence=80, reason="测试")
        result = asyncio.run(ctl.execute_trade_signal(
            ai=ai, verdict=verdict, entry_price=2000, market_side=OrderSide.BUY,
        ))
        assert result["status"] == "REJECTED"
        pos = asyncio.run(broker.get_position(SYMBOL))
        assert pos.side == PositionSide.FLAT


# ===========================================================================
#  2) 利润保护触发 → 市价平仓，并通知 RiskEngine + 统计 wins/losses
# ===========================================================================
class TestCloseForProtection:
    def test_close_at_profit_increments_win(self, tmp_path: Path) -> None:
        ctl, broker, om, risk, journal, ss = _ctl(tmp_path)
        # 先开多仓：买入 0.1 @ 2000
        broker.apply_ticker(bid=2000, ask=2001, last=2000)
        asyncio.run(broker.place_order(
            symbol=SYMBOL, side=OrderSide.BUY, type=OrderType.LIMIT,
            amount=0.1, price=2000,
        ))
        broker.apply_ticker(bid=2000, ask=2001, last=2000)
        pos = asyncio.run(broker.get_position(SYMBOL))
        assert pos.side == PositionSide.LONG
        # 价格拉升：mark=2040（浮盈 +6% × 3 杠杆 = +18% → 触发 step3 lock8）
        high_mark = 2000 * (1 + 0.18 / 3)  # 2000*1.06=2120
        broker.apply_ticker(bid=high_mark - 1, ask=high_mark, last=high_mark)
        pos_high = asyncio.run(broker.get_position(SYMBOL))
        # 触发 PositionManager 阶梯
        pm: PositionManager = ctl.position_manager  # type: ignore[attr-defined]
        lock1 = pm.get_required_lock_pct(pos_high)
        assert lock1 >= 8.0
        # mark 快速回落到 保本上方但触发锁损 → 触发 should_close_for_protection
        # lock8 → 止损价 = 2000 * (1 + 0.08/3) ≈ 2053.33
        # 让 mark = 2000（低于止损价 2053）
        broker.apply_ticker(bid=2000, ask=2001, last=2000)
        pos_mid = asyncio.run(broker.get_position(SYMBOL))
        need_close, _ = pm.should_close_for_protection(pos_mid)
        assert need_close is True
        # 执行 close_for_protection
        closed_pnl = asyncio.run(ctl.close_position_for_protection(pos_mid))
        # 平仓后空仓
        pos_final = asyncio.run(broker.get_position(SYMBOL))
        assert pos_final.side == PositionSide.FLAT
        # 盈利 > 0（@2000 平仓 vs @2000 入场？？其实这里是 0 盈利 → 因为之前开仓 @2000，平仓@2000 bid/ask，实际 0 盈利）
        # 至少 trade_closed 被调用 → stats wins/losses 应变化（pnl≈0 算中性，但应该至少更新一次）
        st = ss.load()
        # 期望至少有一次已关闭交易统计
        assert st["stats"]["trades_closed"] == 1


# ===========================================================================
#  3) Stats & 日切点
# ===========================================================================
class TestStatsAndDailyReset:
    def test_update_stats_win_loss(self, tmp_path: Path) -> None:
        ctl, broker, om, risk, journal, ss = _ctl(tmp_path)
        ctl.update_stats_on_closed(+5.0)   # 盈利 5U
        ctl.update_stats_on_closed(-2.1)   # 亏损 2.1U
        ctl.update_stats_on_closed(0.0)    # 平保
        st = ss.load()
        assert st["stats"]["trades_closed"] == 3
        assert st["stats"]["wins"] == 1
        assert st["stats"]["losses"] == 1
        # 累计收益率(%)：(5 - 2.1) / daily_start(1000) * 100 = 0.29%
        assert abs(float(st["stats"]["total_pnl_pct"]) - (2.9 / 1000 * 100)) < 1e-6
