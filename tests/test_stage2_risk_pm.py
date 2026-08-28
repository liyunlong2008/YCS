# -*- coding: utf-8 -*-
"""阶段 2 补充：RiskEngine 风控 + PositionManager 利润保护 单元测试（TDD）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.base import Position
from app.core.constants import OrderSide, PositionSide, SYMBOL
from app.risk.engine import RiskEngine, RiskVerdict
from app.trading.position_manager import PositionManager, TrailingProfitConfig


# =============================================================================
# RiskEngine 测试
# =============================================================================
class TestRiskEngine:
    def test_default_initial_state(self) -> None:
        r = RiskEngine()
        assert r.consecutive_losses == 0
        assert r.cooldown_until_ts == 0
        assert r.daily_start_balance == 0.0

    def test_to_dict_roundtrip(self) -> None:
        r = RiskEngine()
        r.consecutive_losses = 2
        r.cooldown_until_ts = 100
        r.daily_start_balance = 2500.0
        data = r.to_dict()
        r2 = RiskEngine()
        r2.load_dict(data)
        assert r2.consecutive_losses == 2
        assert r2.cooldown_until_ts == 100
        assert abs(r2.daily_start_balance - 2500.0) < 1e-9

    def test_load_dict_missing_resets_nothing(self) -> None:
        r = RiskEngine()
        r.load_dict(None)
        r.load_dict({})
        assert r.consecutive_losses == 0

    def test_on_trade_closed_accumulates_losses(self) -> None:
        r = RiskEngine()
        r.on_trade_closed(-5.0)  # 亏损
        r.on_trade_closed(-1.2)  # 再亏
        assert r.consecutive_losses == 2
        r.on_trade_closed(+2.0)  # 盈利，清零
        assert r.consecutive_losses == 0

    def test_check_can_open_allows_then_computes_size(self) -> None:
        r = RiskEngine()
        # 1000U 余额，入场价 2000 ETH，默认 RISK_PER_TRADE=1% 亏 10U；止损 1% → 价格跌 20 U
        # Qty = 10U / (20U * 3 lev) = 0.166 张
        v = asyncio.run(r.check_can_open(
            balance_total=1000.0, entry_price=2000.0, now_ts=1_000_000,
        ))
        assert isinstance(v, RiskVerdict)
        assert v.allow is True
        # 建议数量应 > 0
        assert v.suggested_size > 0
        # 止损价 < 入场价（多头基准）
        assert 0 < v.stop_loss_price < 2000
        # 建议杠杆 = DEFAULT_LEVERAGE = 3
        assert v.suggested_leverage == 3
        # 中文 reason 包含关键词
        assert "风控通过" in v.reason

    def test_check_can_open_cooldown_blocks(self) -> None:
        r = RiskEngine()
        now = 1_000_000
        r.cooldown_until_ts = now + 3600  # 1h 后才允许
        v = asyncio.run(r.check_can_open(balance_total=1000, entry_price=2000, now_ts=now))
        assert v.allow is False
        assert "熔断期内" in v.reason

    def test_check_can_open_consecutive_losses_fuse(self) -> None:
        r = RiskEngine()
        r.consecutive_losses = r.MAX_CONSECUTIVE_LOSSES  # 达到阈值
        now = 1_000_000
        v = asyncio.run(r.check_can_open(balance_total=1000, entry_price=2000, now_ts=now))
        # 第一次：立即触发熔断，本次拒绝
        assert v.allow is False
        assert "启动熔断" in v.reason
        # 熔断时间正确
        assert r.cooldown_until_ts == now + r.COOL_DOWN_HOURS * 3600
        # consecutive_losses 已清零
        assert r.consecutive_losses == 0

    def test_check_can_open_daily_loss_fuse(self) -> None:
        r = RiskEngine()
        r.start_new_day(1000.0)
        # 日亏 = 1 - 840/1000 = 16% > 15% → 拒绝
        v = asyncio.run(r.check_can_open(
            balance_total=840.0, entry_price=2000, now_ts=1_000_000,
        ))
        assert v.allow is False
        assert "当日亏损" in v.reason

    def test_check_can_open_low_balance_blocked(self) -> None:
        r = RiskEngine()
        # 余额 10U × 1% = 0.1U；止损 2000 × 1% = 20U × 3 杠杆 → 所需张数极小
        v = asyncio.run(r.check_can_open(
            balance_total=10.0, entry_price=2000, now_ts=1_000_000,
        ))
        assert v.allow is False
        assert "余额不足" in v.reason

    def test_start_new_day_sets_daily_balance(self) -> None:
        r = RiskEngine()
        r.start_new_day(1234.5)
        assert r.daily_start_balance == 1234.5

    def test_orient_stop_loss_long_short(self) -> None:
        # 多：entry=2000, delta=20 → 止损=1980
        assert RiskEngine.orient_stop_loss(2000, 20, OrderSide.BUY) == 1980
        assert RiskEngine.orient_stop_loss(2000, 20, PositionSide.LONG) == 1980
        # 空：entry=2000, delta=20 → 止损=2020
        assert RiskEngine.orient_stop_loss(2000, 20, OrderSide.SELL) == 2020
        assert RiskEngine.orient_stop_loss(2000, 20, PositionSide.SHORT) == 2020


# =============================================================================
# PositionManager 利润保护阶梯 测试
# =============================================================================
class TestPositionManager:
    @staticmethod
    def _pos_long(entry, mark, leverage=3) -> Position:
        return Position(
            symbol=SYMBOL, side=PositionSide.LONG,
            size=1.0, entry_price=entry, mark_price=mark, leverage=leverage,
        )

    @staticmethod
    def _pos_short(entry, mark, leverage=3) -> Position:
        return Position(
            symbol=SYMBOL, side=PositionSide.SHORT,
            size=1.0, entry_price=entry, mark_price=mark, leverage=leverage,
        )

    def test_default_steps_matches_spec(self) -> None:
        cfg = TrailingProfitConfig.default()
        assert cfg.steps == [(3.0, 0.0), (8.0, 3.0), (15.0, 8.0), (30.0, 15.0)]

    def test_pnl_calc_long(self) -> None:
        # entry 2000, mark 2020 (+1% price × 3 lev = +3% 账户)
        p = self._pos_long(2000, 2020, leverage=3)
        assert abs(PositionManager.calc_unrealized_pnl_pct(p) - 3.0) < 1e-9

    def test_pnl_calc_short(self) -> None:
        # entry 2000, mark 1980 (+1% × 3 lev)
        p = self._pos_short(2000, 1980, leverage=3)
        assert abs(PositionManager.calc_unrealized_pnl_pct(p) - 3.0) < 1e-9

    def test_no_trigger_returns_none(self) -> None:
        pm = PositionManager()
        p = self._pos_long(2000, 2005, leverage=3)  # +0.75% 不到 3%
        lock = pm.get_required_lock_pct(p)
        assert lock < -1e8   # 表示无保护

    def test_trigger_step1_breakeven(self) -> None:
        """浮盈 ≥ 3% → 触发保本（锁定 0%）。"""
        pm = PositionManager()
        # entry 2000, mark 需涨到使 pnl_pct=+3% → 价格 +1% * 2000 = 2020 × 3 lev = 3%
        p = self._pos_long(2000, 2020, leverage=3)
        lock = pm.get_required_lock_pct(p)
        assert abs(lock - 0.0) < 1e-9
        # 对应止损价 = entry × (1 + 0%/3) = 2000
        sp = pm.get_trailing_stop_price(p)
        assert abs(sp - 2000.0) < 1e-9

    def test_trigger_step2_lock3pct(self) -> None:
        """浮盈 +8% → 锁 +3%。对多，杠杆3 → 止损价 = entry × (1 + 3%/3) = 2000 × 1.01 = 2020。"""
        pm = PositionManager()
        # pnl = +8% → 价格涨 8/3 % ≈ 2.666% → mark = 2000 × (1 + 0.08/3) ≈ 2053.333
        mark = 2000 * (1 + 0.08 / 3)
        p = self._pos_long(2000, mark, leverage=3)
        lock = pm.get_required_lock_pct(p)
        assert abs(lock - 3.0) < 1e-9
        sp = pm.get_trailing_stop_price(p)
        assert abs(sp - 2000 * (1 + 0.03 / 3)) < 0.1   # ≈ 2020

    def test_lock_only_moves_up_not_down(self) -> None:
        """阶梯只上移不下移：触发到 lock=8% 后，即使浮盈回落到 5%，锁定制约仍保持 8%。"""
        pm = PositionManager()
        # 第一次：浮盈+15% → 锁+8%
        mark_high = 2000 * (1 + 0.15 / 3)
        p_high = self._pos_long(2000, mark_high, leverage=3)
        lock1 = pm.get_required_lock_pct(p_high)
        assert abs(lock1 - 8.0) < 1e-9
        # 第二次：浮盈回落到 +5%（低于触发8%所需的 trigger=8%）
        mark_mid = 2000 * (1 + 0.05 / 3)
        p_mid = self._pos_long(2000, mark_mid, leverage=3)
        lock2 = pm.get_required_lock_pct(p_mid)
        # 锁定仍保持 8%（只涨不跌）
        assert abs(lock2 - 8.0) < 1e-9

    def test_reset_on_flat(self) -> None:
        pm = PositionManager()
        # 先触发保本
        p = self._pos_long(2000, 2020, leverage=3)
        pm.get_required_lock_pct(p)
        assert pm._current_lock_pct == 0.0
        # 空仓 → reset
        flat = Position(symbol=SYMBOL, side=PositionSide.FLAT)
        pm.get_required_lock_pct(flat)
        assert pm._current_lock_pct is None

    def test_should_close_no_trigger_returns_false(self) -> None:
        pm = PositionManager()
        p = self._pos_long(2000, 2010, leverage=3)  # +1.5%
        close, reason = pm.should_close_for_protection(p)
        assert close is False
        assert "尚未触发" in reason

    def test_should_close_triggered_when_drops_below_lock(self) -> None:
        """触发保本后，mark 跌到入场价以下 → should_close=True。"""
        pm = PositionManager()
        # 先拉高 mark 触发保本 lock=0%
        p_up = self._pos_long(2000, 2020, leverage=3)
        pm.get_required_lock_pct(p_up)
        # mark 回落到 1998（低于保本止损 2000）
        p_down = self._pos_long(2000, 1998, leverage=3)
        close, reason = pm.should_close_for_protection(p_down)
        assert close is True
        assert "利润保护触发（多）" in reason
        assert "跌破锁定止损价" in reason

    def test_short_trailing_stop_direction(self) -> None:
        """空仓：lock_pct > 0 → 止损价 > entry；mark 涨破止损价 → 应平仓。"""
        pm = PositionManager()
        # 空浮盈 +8%（lev=3 → 价格跌 8/3%）
        mark_down = 2000 * (1 - 0.08 / 3)
        p = self._pos_short(2000, mark_down, leverage=3)
        lock = pm.get_required_lock_pct(p)
        assert abs(lock - 3.0) < 1e-9
        # 锁定止损价 = entry × (1 - lock_pct/lev) = 2000 × (1 - 0.01) = 1980
        sp = pm.get_trailing_stop_price(p)
        assert abs(sp - 1980.0) < 1e-6
        # mark 反弹到 2000（> 止损价 1980） → 应平空
        p_up = self._pos_short(2000, 2000, leverage=3)
        close, reason = pm.should_close_for_protection(p_up)
        assert close is True
        assert "利润保护触发（空）" in reason
        assert "涨破锁定止损价" in reason

    def test_save_load_roundtrip(self) -> None:
        pm = PositionManager()
        # 触发 step1
        p = self._pos_long(2000, 2020, leverage=3)
        pm.get_required_lock_pct(p)
        data = pm.to_dict()
        assert data["current_lock_pct"] == 0.0
        pm2 = PositionManager()
        pm2.load_dict(data)
        assert pm2._current_lock_pct == 0.0

    def test_reset_method(self) -> None:
        pm = PositionManager()
        pm._current_lock_pct = 8.0
        pm.reset()
        assert pm._current_lock_pct is None
