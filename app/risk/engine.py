# -*- coding: utf-8 -*-
"""风控引擎实现（设计文档 · 第十节）。

最高权限模块：
  - 是否允许开仓
  - 仓位大小 / 杠杆
  - 止损计算
  - 熔断控制

规则：
  - 连续亏损 3 次 → 暂停 12 小时
  - 每日亏损 15%   → 停止交易
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel


class RiskVerdict(BaseModel):
    """风控判决结果。"""
    allow: bool                  # 是否允许执行
    reason: str = ""             # 原因（中文）
    suggested_size: float = 0.0  # 允许的下单数量
    suggested_leverage: int = 1  # 建议杠杆
    stop_loss_price: float = 0.0 # 建议止损价


class RiskEngine:
    """风控引擎（占位实现，阶段 2 填充）。"""

    # 连续亏损熔断阈值 / 暂停时长（小时）
    MAX_CONSECUTIVE_LOSSES = 3
    COOL_DOWN_HOURS = 12

    # 日亏损熔断（百分比）
    MAX_DAILY_LOSS_PCT = 15

    def __init__(self) -> None:
        self.consecutive_losses: int = 0
        self.cooldown_until_ts: int = 0   # 熔断解除 Unix 秒时间戳
        self.daily_start_balance: float = 0.0

    # ------------------------------------------------------------------
    async def check_can_open(
        self,
        *,
        balance_total: float,
        current_pnl_pct: float,
        now_ts: int,
    ) -> RiskVerdict:
        """开仓前风控检查。占位实现：默认允许小额。"""
        # 熔断期内
        if now_ts < self.cooldown_until_ts:
            return RiskVerdict(allow=False, reason="连续亏损熔断期内，暂停开仓")

        # 连续亏损达到阈值 → 熔断
        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self.cooldown_until_ts = now_ts + self.COOL_DOWN_HOURS * 3600
            self.consecutive_losses = 0
            return RiskVerdict(allow=False, reason=f"连续亏损 {self.MAX_CONSECUTIVE_LOSSES} 次，熔断 {self.COOL_DOWN_HOURS} 小时")

        # 日亏损熔断
        if self.daily_start_balance > 0:
            daily_loss_pct = (1 - balance_total / self.daily_start_balance) * 100
            if daily_loss_pct >= self.MAX_DAILY_LOSS_PCT:
                return RiskVerdict(allow=False, reason=f"当日亏损 {daily_loss_pct:.1f}%，超过 {self.MAX_DAILY_LOSS_PCT}% 阈值")

        return RiskVerdict(
            allow=True,
            reason="风控通过",
            suggested_size=0.0,         # 阶段 2 结合凯利 / 固定比例计算
            suggested_leverage=3,
            stop_loss_price=0.0,
        )

    # ------------------------------------------------------------------
    def on_trade_closed(self, pnl_pct: float) -> None:
        """平仓回调：累计连续亏损、重置。"""
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
