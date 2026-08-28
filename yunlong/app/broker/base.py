# -*- coding: utf-8 -*-
"""Broker 统一抽象（设计文档 · 第八节）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field

from ..core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)


class Balance(BaseModel):
    """账户余额（USDT）。"""
    total: float = 0.0        # 账户总权益
    available: float = 0.0    # 可用保证金
    unrealized_pnl: float = 0.0  # 未实现盈亏


class Position(BaseModel):
    """持仓信息（V1 单仓位）。"""
    symbol: str
    side: PositionSide = PositionSide.FLAT
    size: float = 0.0         # 合约张数
    entry_price: float = 0.0  # 开仓均价
    mark_price: float = 0.0   # 标记价格
    unrealized_pnl: float = 0.0
    leverage: int = 1
    liquidation_price: float = 0.0


class Order(BaseModel):
    """订单对象。"""
    client_order_id: str                          # YL-YYYYMMDD-XXXXX
    order_id: str = ""                            # 交易所返回 ID
    symbol: str
    side: OrderSide
    type: OrderType
    price: float = 0.0
    amount: float = 0.0                           # 下单数量
    filled: float = 0.0                           # 已成交数量
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: int = Field(default_factory=lambda: __import__("time").time_ns() // 1_000_000)
    updated_at: int = created_at


class Broker(ABC):
    """Broker 统一接口。

    子类：
      - PaperBroker：本地模拟成交
      - OKXBroker：调用 OKX 实盘接口
    """

    # ------------------------------------------------------------------
    # 行情 / 状态
    # ------------------------------------------------------------------
    @abstractmethod
    async def get_server_time_ms(self) -> int:
        """获取交易所服务器时间（毫秒）。"""

    @abstractmethod
    async def get_balance(self) -> Balance:
        """查询账户余额。"""

    @abstractmethod
    async def get_position(self, symbol: str) -> Position:
        """查询当前持仓（V1 单仓位）。"""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[Order]:
        """查询当前未成交订单。"""

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """提交订单。

        Args:
            symbol: 交易对（如 ETH-USDT-SWAP）
            side: BUY / SELL
            type: LIMIT / MARKET / STOP
            amount: 下单数量（合约张数）
            price: 限价单价格；市价单可为 0
            client_order_id: 客户端自定义订单号（幂等 / 恢复用）
        """

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        """撤销订单。成功返回 True。"""
