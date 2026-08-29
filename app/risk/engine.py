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
  - 单笔风险：默认 1% 账户总权益（R = 1%）
  - 默认止损：-1%（无杠杆），对应用 (1% * leverage) 的收益率止损
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..core.constants import OrderSide, PositionSide


class RiskVerdict(BaseModel):
    """风控判决结果。"""
    allow: bool                  # 是否允许执行
    reason: str = ""             # 原因（中文）
    suggested_size: float = 0.0  # 允许的下单数量（合约张数 / ETH-USDT-SWAP 1 张 = 0.01 BTC 等价 ETH 合约，此处简化 amount 为 ETH 数量张数）
    suggested_leverage: int = 1  # 建议杠杆
    stop_loss_price: float = 0.0 # 建议止损价


class RiskEngine:
    """风控引擎（完整实现）。"""

    # ------------------------------------------------------------------
    # 熔断阈值
    # ------------------------------------------------------------------
    MAX_CONSECUTIVE_LOSSES = 3
    COOL_DOWN_HOURS = 12
    MAX_DAILY_LOSS_PCT = 15

    # 单笔风险：每笔最大亏损 = 账户总权益 × RISK_PER_TRADE_PCT（默认 1R = 1%）
    RISK_PER_TRADE_PCT = 1.0

    # 默认止损（相对于入场价的价格百分比，无杠杆）。
    # 如 1.0 → 价格跌 1% 即触发止损（× leverage = 对账户 = 1×leverage % 亏损）
    DEFAULT_STOP_LOSS_PRICE_PCT = 1.0

    # 单笔建议杠杆（阶段 1 默认 3x）
    DEFAULT_LEVERAGE = 3

    # 最小下单张数（ETH-USDT-SWAP 最小约 0.1 张，此处取 0.1）
    MIN_ORDER_SIZE = 0.1

    def __init__(self) -> None:
        self.consecutive_losses: int = 0
        self.cooldown_until_ts: int = 0   # 熔断解除 Unix 秒时间戳
        self.daily_start_balance: float = 0.0

    # ------------------------------------------------------------------
    # 状态持久化（与 StateStore["risk"] 对接）
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """导出风控状态，供 StateStore 持久化。"""
        return {
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until_ts": self.cooldown_until_ts,
            "daily_start_balance": self.daily_start_balance,
        }

    def load_dict(self, data: dict[str, Any] | None) -> None:
        """从 StateStore 恢复风控状态。"""
        if not data:
            return
        try:
            self.consecutive_losses = int(data.get("consecutive_losses", 0))
            self.cooldown_until_ts = int(data.get("cooldown_until_ts", 0))
            self.daily_start_balance = float(data.get("daily_start_balance", 0.0))
        except Exception:
            # 字段损坏时不阻塞，等价于冷启动
            self.consecutive_losses = 0
            self.cooldown_until_ts = 0
            self.daily_start_balance = 0.0

    # ------------------------------------------------------------------
    # 日切点 / 平仓结果回调
    # ------------------------------------------------------------------
    def start_new_day(self, current_balance_total: float) -> None:
        """每个交易日开始（或首次启动）调用，固定日初权益。"""
        self.daily_start_balance = float(current_balance_total)

    def on_trade_closed(self, pnl_pct: float) -> None:
        """平仓回调：累计连续亏损、重置。

        Args:
            pnl_pct: 该笔已实现盈亏（百分比，正为盈利，负为亏损）。
        """
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ------------------------------------------------------------------
    # A2. 每日亏损熔断（USDT 绝对值：realized + unrealized 合计 ≤ -limit 立即 HALT）
    #     比百分比更稳，适合 < 50U 超小账户（14.8U 用户现在就是这种情况）
    # ------------------------------------------------------------------
    def check_absolute_daily_loss(
        self,
        *,
        total_now: float,
        realized_pnl_usdt: float,
        unrealized_pnl_usdt: float,
        limit_usdt: float,
    ) -> tuple[bool, str]:
        """A2 绝对日损熔断。

        Args:
            total_now: 当前账户总权益（仅用于日志上下文展示）
            realized_pnl_usdt: 已实现盈亏（当日平仓合计）
            unrealized_pnl_usdt: 未实现浮动盈亏（当前持仓）
            limit_usdt: 阈值（正数，例如 3.0 = 最多允许亏 3 USDT）

        Returns:
            (allow: bool, reason: str) → allow=False 表示已触发 HALT。
        """
        limit = abs(float(limit_usdt or 0))
        realized = float(realized_pnl_usdt or 0)
        unrealized = float(unrealized_pnl_usdt or 0)
        total_loss = realized + unrealized          # 正常为负数
        if limit > 0 and total_loss <= -limit:
            msg = (
                f"[A2 HALT] 当日合计亏损 {total_loss:.4f} U ≤ -{limit:.4f} U 阈值："
                f"已实现 {realized:.4f} U + 未实现 {unrealized:.4f} U；当前权益 {float(total_now or 0):.4f} U。"
                "立即全平 + 停机，待次日手动解除。"
            )
            # 同步熔断：后续 check_can_open 也直接挡
            self.cooldown_until_ts = int(__import__("time").time()) + 86_400  # 直接冻 24 小时
            return False, msg
        return True, (
            f"日损监控：合计 {realized + unrealized:.4f} U / 阈值 -{limit:.4f} U（正常）。"
        )

    # ------------------------------------------------------------------
    # 风控主入口
    # ------------------------------------------------------------------
    async def check_can_open(
        self,
        *,
        balance_total: float,
        balance_available: float | None = None,
        entry_price: float = 2000.0,  # 默认 ETH 参考价，仅用于仓位估算
        now_ts: int,
        current_pnl_pct: float = 0.0,  # 兼容：旧调用方传入；暂未使用，保留用于未来日中波动熔断
    ) -> RiskVerdict:
        """开仓前风控：熔断 / 日亏 / 仓位 / 杠杆 / 止损 全量计算。

        Args:
            balance_total: 账户总权益
            balance_available: 可用保证金（缺省时用 total 的 90%）
            entry_price: 预期入场价
            now_ts: 当前秒时间戳

        Returns:
            RiskVerdict，包含 allow / 中文 reason / 建议 size / 杠杆 / 止损价。
        """
        leverage = self.DEFAULT_LEVERAGE
        avail = balance_available if (balance_available is not None and balance_available > 0) else balance_total * 0.9

        # 1) 熔断期内
        if now_ts < self.cooldown_until_ts:
            remain_h = (self.cooldown_until_ts - now_ts) / 3600
            return RiskVerdict(
                allow=False,
                reason=f"连续亏损熔断期内，剩余 {remain_h:.1f} 小时，暂停开仓",
                suggested_leverage=leverage,
            )

        # 2) 连续亏损达到阈值 → 立即熔断，下次 check 起生效
        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self.cooldown_until_ts = now_ts + self.COOL_DOWN_HOURS * 3600
            self.consecutive_losses = 0
            return RiskVerdict(
                allow=False,
                reason=f"连续亏损 {self.MAX_CONSECUTIVE_LOSSES} 次，启动熔断 {self.COOL_DOWN_HOURS} 小时",
                suggested_leverage=leverage,
            )

        # 3) 日亏损熔断
        daily_loss_pct = 0.0
        if self.daily_start_balance > 1e-9:
            daily_loss_pct = (1 - balance_total / self.daily_start_balance) * 100
            if daily_loss_pct >= self.MAX_DAILY_LOSS_PCT:
                return RiskVerdict(
                    allow=False,
                    reason=(
                        f"当日亏损 {daily_loss_pct:.2f}% 超过阈值 {self.MAX_DAILY_LOSS_PCT}%，"
                        "强制停止交易，请次日再启动"
                    ),
                    suggested_leverage=leverage,
                )

        # 4) 按 R 计算仓位：每笔最大亏损 = total × (RISK_PER_TRADE_PCT / 100)
        #    绝对止损 = entry_price × (DEFAULT_STOP_LOSS_PRICE_PCT / 100)
        #    → 张数 Qty = 最大可亏损资金 / (绝对止损 × leverage)
        #    名义价值约束（≤ 可用保证金 × 杠杆）
        max_loss_usdt = balance_total * (self.RISK_PER_TRADE_PCT / 100.0)
        sl_price_delta = max(entry_price * (self.DEFAULT_STOP_LOSS_PRICE_PCT / 100.0), 0.01)
        stop_loss_price = max(0.01, entry_price - sl_price_delta)  # 默认按多空再调，此处给多头基准
        qty_by_risk = max_loss_usdt / max(sl_price_delta * leverage, 1e-9)
        qty_by_margin = (avail * leverage) / max(entry_price, 1e-9)
        qty = min(qty_by_risk, qty_by_margin)
        if qty < self.MIN_ORDER_SIZE:
            return RiskVerdict(
                allow=False,
                reason=(
                    f"余额不足（total={balance_total:.2f} 可下张数={qty:.3f} "
                    f"< 最小 {self.MIN_ORDER_SIZE}）"
                ),
                suggested_leverage=leverage,
                stop_loss_price=stop_loss_price,
            )
        # 张数截断到 0.01（避免小数过长）
        qty = float(int(qty * 100) / 100.0)
        qty = max(qty, self.MIN_ORDER_SIZE)

        return RiskVerdict(
            allow=True,
            reason=(
                f"风控通过：连亏={self.consecutive_losses} 日亏={daily_loss_pct:.2f}% "
                f"单R={self.RISK_PER_TRADE_PCT}% 止损=±{self.DEFAULT_STOP_LOSS_PRICE_PCT}%"
            ),
            suggested_size=qty,
            suggested_leverage=leverage,
            stop_loss_price=stop_loss_price,
        )

    # ------------------------------------------------------------------
    # 辅助：按方向返回正确止损价（check_can_open 返回的是多头基准）
    # ------------------------------------------------------------------
    @staticmethod
    def orient_stop_loss(entry_price: float, sl_price_delta: float, side: OrderSide | PositionSide) -> float:
        """对多头：止损低于入场价；对空头：止损高于入场价。"""
        if side in (OrderSide.BUY, PositionSide.LONG):
            return max(0.01, entry_price - sl_price_delta)
        return entry_price + sl_price_delta
