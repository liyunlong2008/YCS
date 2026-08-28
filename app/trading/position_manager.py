# -*- coding: utf-8 -*-
"""仓位管理器 + 利润保护（阶梯移动止损，设计文档 · 第十三节）。

利润保护阶梯（默认）：
  账面浮盈 ≥ +3%  → 止损上移至保本（锁定 ≥ 0%）
  账面浮盈 ≥ +8%  → 锁定 ≥ +3%
  账面浮盈 ≥ +15% → 锁定 ≥ +8%
  账面浮盈 ≥ +30% → 锁定 ≥ +15%

利润保护仅在持有持仓时生效，空仓时状态重置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..broker.base import Position
from ..core.constants import PositionSide


@dataclass
class TrailingProfitConfig:
    """阶梯利润保护参数（百分比）。

    steps = list[(trigger_pct, lock_pct)]
      - trigger_pct: 账面浮盈达到此值（%）时，触发上移止损
      - lock_pct: 上移止损后，最低必须锁定的收益率（%）
    """
    steps: list[tuple[float, float]]

    @classmethod
    def default(cls) -> "TrailingProfitConfig":
        """与设计文档第十三节表格完全一致。"""
        return cls(steps=[
            (3.0,  0.0),   # 盈利 3% → 保本（锁定0%）
            (8.0,  3.0),   # 盈利 8% → 锁定 +3%
            (15.0, 8.0),   # 盈利15% → 锁定 +8%
            (30.0, 15.0),  # 盈利30% → 锁定 +15%
        ])


class PositionManager:
    """仓位 / 利润保护管理器（完整实现）。"""

    def __init__(self, config: TrailingProfitConfig | None = None) -> None:
        self.config = config or TrailingProfitConfig.default()
        # 当前已生效的锁定收益率（%）：None 表示尚未触发任何阶梯
        self._current_lock_pct: float | None = None

    # ------------------------------------------------------------------
    # 状态持久化（与 StateStore 对接）
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "current_lock_pct": self._current_lock_pct,
            "steps": [[a, b] for a, b in self.config.steps],
        }

    def load_dict(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        try:
            lock = data.get("current_lock_pct")
            self._current_lock_pct = float(lock) if lock is not None else None
            steps = data.get("steps")
            if isinstance(steps, list) and len(steps) > 0:
                self.config = TrailingProfitConfig(
                    steps=[(float(a), float(b)) for a, b in steps]
                )
        except Exception:
            # 损坏字段：冷启动重置
            self._current_lock_pct = None

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """空仓 / 系统重启无持仓时调用：清零已锁定阶梯。"""
        if self._current_lock_pct is not None:
            logger.info("利润保护状态重置（空仓）：原锁定 {:.2f}%", self._current_lock_pct)
        self._current_lock_pct = None

    # ------------------------------------------------------------------
    @staticmethod
    def calc_unrealized_pnl_pct(position: Position) -> float:
        """计算未实现收益率（%，乘以杠杆后的账户收益率）。"""
        if position.side == PositionSide.FLAT or position.entry_price <= 0:
            return 0.0
        lev = max(1, position.leverage)
        if position.side == PositionSide.LONG:
            delta = position.mark_price - position.entry_price
        else:  # SHORT
            delta = position.entry_price - position.mark_price
        return (delta / position.entry_price) * 100 * lev

    # ------------------------------------------------------------------
    def get_required_lock_pct(self, position: Position) -> float:
        """根据当前浮盈和阶梯，返回**必须保留的最低收益率（%）**。

        说明：
          - 未触发任何阶梯：返回 -1e9（表示没有保护，允许回撤到最初止损）
          - 触发后：单向提高（只涨不跌），返回 0 / 3 / 8 / 15 等。
        """
        if position.side == PositionSide.FLAT:
            self.reset()
            return -1e9

        pnl = self.calc_unrealized_pnl_pct(position)
        best_lock: float = -1e9
        # 阶梯按 trigger 升序遍历；加入 1e-6 的浮点容差，避免 float 精度导致恰好落在档位边界时漏触发
        _EPS = 1e-6
        for trigger, lock_pct in self.config.steps:
            if pnl + _EPS >= trigger:
                best_lock = max(best_lock, lock_pct)
            else:
                break
        # 只接受更高的锁定制约，防止高位触发后回撤又取消保护
        if best_lock > -1e9:
            if self._current_lock_pct is None or best_lock > self._current_lock_pct:
                logger.info(
                    "利润保护阶梯触发：当前浮盈 {:.2f}%，上移锁定收益率 ≥ {:.2f}%（steps={}）",
                    pnl, best_lock, self.config.steps,
                )
                self._current_lock_pct = best_lock
        return self._current_lock_pct if self._current_lock_pct is not None else -1e9

    # 兼容别名（旧代码 / 旧测试使用 get_required_stop_pct）
    get_required_stop_pct = get_required_lock_pct

    # ------------------------------------------------------------------
    def get_trailing_stop_price(self, position: Position) -> float:
        """根据当前持仓方向和已锁定制约，换算出绝对移动止损价。

        空仓时返回 0.0；未触发阶梯时返回 0.0（交由 RiskEngine 的初始止损）。
        """
        lock_pct = self.get_required_lock_pct(position)
        if lock_pct <= -1e9 or position.side == PositionSide.FLAT or position.entry_price <= 0:
            return 0.0

        lev = max(1, position.leverage)
        # lock_pct 是对账户（带杠杆）的最低收益率。换算成价格变动百分比：
        #   pnl_pct = (price_delta / entry_price) * 100 * lev = lock_pct
        #   → price_delta = (lock_pct / 100 / lev) * entry_price
        price_delta_pct = lock_pct / 100.0 / lev
        if position.side == PositionSide.LONG:
            # 多：止损价 = 入场价 × (1 + price_delta_pct)
            return position.entry_price * (1.0 + price_delta_pct)
        else:
            # 空：方向相反
            return position.entry_price * (1.0 - price_delta_pct)

    # ------------------------------------------------------------------
    def should_close_for_protection(self, position: Position) -> tuple[bool, str]:
        """判断是否需要因「利润保护」平仓。

        Returns:
            (是否应平仓, 中文原因说明)
        """
        if position.side == PositionSide.FLAT:
            return False, "空仓，无需保护"

        lock_price = self.get_trailing_stop_price(position)
        if lock_price <= 0:
            return False, "尚未触发利润保护阶梯"

        if position.side == PositionSide.LONG and position.mark_price <= lock_price:
            pnl = self.calc_unrealized_pnl_pct(position)
            return True, (
                f"利润保护触发（多）：当前浮盈 {pnl:.2f}% 回撤，跌破锁定止损价 {lock_price:.2f}，立即止盈平仓"
            )
        if position.side == PositionSide.SHORT and position.mark_price >= lock_price:
            pnl = self.calc_unrealized_pnl_pct(position)
            return True, (
                f"利润保护触发（空）：当前浮盈 {pnl:.2f}% 回撤，涨破锁定止损价 {lock_price:.2f}，立即止盈平仓"
            )

        return False, "未触发利润保护止损线"
