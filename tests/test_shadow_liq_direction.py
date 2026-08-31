"""BD2a: ShadowBroker 空单强平价 Bug 回归单测
复现巡检报告：SHORT @ 2446 / lev10 → liq 算成 4642（近2倍现价），正确应≈2691（1+1/lev）。"""
from __future__ import annotations
import pytest

class Test_ShadowLiqPriceDirection:
    def test_short_lev10_liq_must_be_above_entry_but_less_than_2x(self):
        """空单：price ↑ 才爆仓 → liq ∈ (entry, entry*1.5]。绝对不能达到 1.9×entry（接近破产）。"""
        from app.broker.shadow import ShadowBroker
        from app.core.constants import PositionSide
        entry = 2446.39
        lev = 10
        liq = ShadowBroker._est_liquidation_price(
            None,  # self 不被用到（静态逻辑）
            side=PositionSide.SHORT, size_sz=0.1,
            entry_price=entry, leverage=lev, ct_val=0.01,
        )
        # SHORT: liq ∈ (entry, entry*(1+3/lev)) — 距 entry 越远越离谱，最大 1+3/lev ≈ 1.3x
        assert entry < liq <= entry * (1 + 2.0/lev), (
            f"SHORT 强平价错误: liq={liq:.2f}, entry={entry:.2f}, lev={lev}. "
            f"正确范围: ({entry:.2f}, {entry*(1+2/lev):.2f}]"
        )

    def test_long_lev10_liq_must_be_below_entry_but_positive(self):
        """多单：price ↓ 才爆仓 → liq ∈ [entry*0.7, entry)。"""
        from app.broker.shadow import ShadowBroker
        from app.core.constants import PositionSide
        entry = 2446.39
        lev = 10
        liq = ShadowBroker._est_liquidation_price(
            None, side=PositionSide.LONG, size_sz=0.1,
            entry_price=entry, leverage=lev, ct_val=0.01,
        )
        assert entry * (1 - 2.0/lev) <= liq < entry, (
            f"LONG 强平价错误: liq={liq:.2f}, entry={entry:.2f}, lev={lev}. "
            f"正确范围: [{entry*(1-2/lev):.2f}, {entry:.2f})"
        )

    def test_extreme_high_lev_short_still_positive_and_above_entry(self):
        """极端高杠杆 25X：SHORT liq 仍然 > entry（但比 lev10 更靠近 entry）。"""
        from app.broker.shadow import ShadowBroker
        from app.core.constants import PositionSide
        entry = 2500.0
        lev = 25
        liq = ShadowBroker._est_liquidation_price(
            None, side=PositionSide.SHORT, size_sz=1.0,
            entry_price=entry, leverage=lev, ct_val=0.01,
        )
        ideal = entry * (1 + 1/lev)  # = 2600
        # 容差 ±4%（考虑 MM buffer）
        assert abs(liq - ideal) / ideal < 0.10, (
            f"SHORT lev25: 理想≈{ideal:.2f}，实际={liq:.2f}，偏差超过 10%"
        )

    def test_flat_returns_zero(self):
        """空仓 → liq = 0。"""
        from app.broker.shadow import ShadowBroker
        from app.core.constants import PositionSide
        liq = ShadowBroker._est_liquidation_price(
            None, side=PositionSide.FLAT, size_sz=0.0,
            entry_price=1000.0, leverage=5, ct_val=0.01,
        )
        assert liq == 0.0
