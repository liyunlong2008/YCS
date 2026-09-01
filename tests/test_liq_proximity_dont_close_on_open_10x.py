"""RED→GREEN 现场紧急 Bug：10X 任何新仓立刻被『强平邻近保护』错平。

现场 journal:
  Sep 01 08:16:10  BUY 0.1 entry=2471.05
  Sep 01 08:16:22  距离 9.74% < 阈值 20.00% → 主动全平（12s 后！）

根因：thr_pct=20%（距离 < 20% 就平），但对 10X 多头，开仓瞬间安全距离就≈10%（
entry - liq ≈ entry * (1/10) = 10% price distance），所以一开仓就必触发，
死循环：启动→RUNNING→开仓→秒平→HALT→重启→…

修复思路：把 thr_pct 的语义从『距强平价绝对百分比(mark price)』改为
『初始安全缓冲已消耗百分比(relative buffer consumed)』= 100% × (price_move_into_liq_direction / initial_buffer)。
初始缓冲 = |entry - liq|。开仓时消耗 0% → 不会平；真的快爆（消耗 >85%）才平。

另外 Bug 2（隐式）：run.py L479 await ctl.kill_switch(...) 但 controller 无该方法，
抛 AttributeError 被外层 except 吞，日志不显示；改为 state_store 直接写 HALT。
"""

from __future__ import annotations

import pytest
from app.core.constants import PositionSide
from app.services.controller import TradingController as TC


class Test_LiqProximity_DoesNotTrigger_On_Fresh_10x_Open:
    """10X 新仓开仓瞬间（mark≈entry，buffer_consumed≈0%）→ 绝对不能触发平仓。"""

    @staticmethod
    def test_10x_long_fresh_mark_equals_entry_no_close():
        """现场镜像：LONG entry=2471.05 mark≈2470.66 lev=10 liq=2230.12。
        距离=9.74%（旧逻辑 20% 阈值 = 触发 WRONG True），
        新逻辑 buffer_consumed=|2471.05-2470.66|/|2471.05-2230.12| ≈ 0.39/240.93 ≈ 0.16%，应 False。"""
        close, reason = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=2470.66, entry_price=2471.05, liq_price=2230.12, leverage=10,
        )
        assert close is False, (
            f"10X 新仓瞬间错误触发主动平仓！reason={reason}"
            "（必须改用『缓冲消耗率』语义，不拿绝对值 20% 比 10%）"
        )

    @staticmethod
    def test_10x_short_fresh_mark_equals_entry_no_close():
        entry = 2470.0; mark = 2470.5; lev = 10
        # SHORT liq = entry*(1 + 1/10) = 2717
        liq = entry * 1.10
        close, reason = TC.is_liq_proximity_close(
            PositionSide.SHORT,
            mark_price=mark, entry_price=entry, liq_price=liq, leverage=lev,
        )
        assert close is False, (
            f"10X SHORT 新仓瞬间错误触发！reason={reason}"
        )

    @staticmethod
    def test_10x_long_buffer_90_consumed_does_close():
        """真正危险：初始缓冲 240，已吃 216 → 剩 24（90% 消耗）→ 必须触发。"""
        entry = 2471.05; liq = 2230.12
        initial_buf = entry - liq  # ≈ 240.93
        # 90% 消耗 → mark 跌到 entry - 0.9 * initial_buf
        mark = entry - 0.90 * initial_buf
        close, reason = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=mark, entry_price=entry, liq_price=liq, leverage=10,
        )
        assert close is True, (
            f"10X LONG 90% 缓冲消耗未触发平（应触发>85%阈值）！mark={mark:.2f} reason={reason}"
        )
        assert "85" in reason or "缓冲" in reason or "消耗" in reason, (
            "reason 应提示『缓冲消耗率』阈值，不再提绝对 20%"
        )

    @staticmethod
    def test_10x_long_buffer_60_consumed_stays_open():
        entry = 2471.05; liq = 2230.12
        initial_buf = entry - liq
        mark = entry - 0.60 * initial_buf  # 吃 60%，还剩 40% ≈ 96 点
        close, reason = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=mark, entry_price=entry, liq_price=liq, leverage=10,
        )
        assert close is False, (
            f"10X LONG 仅吃 60% 缓冲不应触发！mark={mark:.2f} reason={reason}"
        )

    @staticmethod
    def test_5x_long_fresh_mark_equals_entry_no_close():
        """5X 初始缓冲 20%（远大于老的 20% 阈值 = 边界），新仓仍不得触发。"""
        entry = 2471.05; lev = 5
        liq = entry * (1 - 1/5)  # 1976.84
        mark = entry - 1.0
        close, reason = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=mark, entry_price=entry, liq_price=liq, leverage=lev,
        )
        assert close is False, (
            f"5X LONG 新仓不应触发！reason={reason}"
        )

    @staticmethod
    def test_liq_price_zero_fallback_matches_entry_over_lev():
        """liq=0 兜底估算也要用『初始缓冲 = entry/lev』。"""
        entry = 2471.0; lev = 10
        close, _ = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=2470.5, entry_price=entry, liq_price=0, leverage=lev,
        )
        assert close is False, "liq=0 兜底估算的 10X 新仓仍错误触发"

    @staticmethod
    def test_flat_returns_false():
        close, reason = TC.is_liq_proximity_close(
            PositionSide.FLAT, mark_price=0, entry_price=0, liq_price=0, leverage=1,
        )
        assert close is False


class Test_LiqProximity_Crosses_Actual_Liquidation:
    """mark <= liq 应触发（真爆仓前最后一层保险）。"""

    @staticmethod
    def test_long_mark_below_liq_forces_close():
        close, reason = TC.is_liq_proximity_close(
            PositionSide.LONG,
            mark_price=2220.0, entry_price=2471.05, liq_price=2230.12, leverage=10,
        )
        assert close is True

    @staticmethod
    def test_short_mark_above_liq_forces_close():
        close, reason = TC.is_liq_proximity_close(
            PositionSide.SHORT,
            mark_price=2720.0, entry_price=2471.0, liq_price=2718.10, leverage=10,
        )
        assert close is True
