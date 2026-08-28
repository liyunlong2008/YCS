# -*- coding: utf-8 -*-
"""仓位管理器 + 利润保护（阶梯移动止损，设计文档 · 第十三节）。

利润保护阶梯：
  盈利 +3%  → 移动至保本
  盈利 +8%  → 锁定 +3%
  盈利 +15% → 锁定 +8%
  盈利 +30% → 锁定 +15%
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from ..broker.base import Position
from ..core.constants import PositionSide


@dataclass
class TrailingProfitConfig:
    """阶梯利润保护参数（百分比）。"""
    # 触发阈值：账面浮盈达到 trigger 时，将止损上移至 lock 水平
    steps: list[tuple[float, float]]
    # (3, 0)   → 盈利 3%，止损到 0%（保本）
    # (8, 3)   → 盈利 8%，止损到 +3%
    # (15, 8)  → 盈利 15%，止损到 +8%
    # (30, 15) → 盈利 30%，止损到 +15%

    @classmethod
    def default(cls) -> "TrailingProfitConfig":
        return cls(steps=[(3.0, 0.0), (8.0, 3.0), (15.0, 8.0), (30.0, 15.0)])


class PositionManager:
    """仓位 / 利润保护管理器（占位实现，阶段 2 填充）。"""

    def __init__(self, config: TrailingProfitConfig | None = None) -> None:
        self.config = config or TrailingProfitConfig.default()
        # 每个方向目前生效的锁定收益率（%），初始 None 表示未触发
        self._current_lock_pct: float | None = None

    # ------------------------------------------------------------------
    @staticmethod
    def calc_unrealized_pnl_pct(position: Position) -> float:
        """计算未实现收益率（%）。"""
        if position.side == PositionSide.FLAT or position.entry_price <= 0:
            return 0.0
        if position.side == PositionSide.LONG:
            delta = position.mark_price - position.entry_price
        else:
            delta = position.entry_price - position.mark_price
        leverage = max(1, position.leverage)
        return (delta / position.entry_price) * 100 * leverage

    # ------------------------------------------------------------------
    def get_required_stop_pct(self, position: Position) -> float:
        """根据当前浮盈和阶梯，返回应锁定的最低收益率（%）。

        - 未触发阶梯：返回 -inf，表示允许回撤到原始止损
        - 触发后：返回应锁定的收益率（0 / 3 / 8 / 15 …）
        """
        pnl = self.calc_unrealized_pnl_pct(position)
        lock: float = -1e9
        for trigger, lock_pct in self.config.steps:
            if pnl >= trigger:
                lock = max(lock, lock_pct)
            else:
                break
        if lock > -1e9:
            if self._current_lock_pct is None or lock > self._current_lock_pct:
                logger.info("利润保护触发：浮盈 {:.2f}%，锁定 ≥ {:.2f}%", pnl, lock)
                self._current_lock_pct = lock
        return self._current_lock_pct if self._current_lock_pct is not None else -1e9
