# -*- coding: utf-8 -*-
"""PaperBroker：本地模拟成交（设计文档 · 第八/三节）。

阶段 3 主力实现，当前仅保留最小骨架。
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from ..core.constants import OrderSide, OrderStatus, OrderType, PositionSide, SYMBOL
from .base import Balance, Broker, Order, Position


class PaperBroker(Broker):
    """本地模拟成交 Broker。占位实现，阶段 3 填充。"""

    def __init__(self, symbol: str = SYMBOL) -> None:
        self.symbol = symbol
        # 模拟初始余额 1000 USDT，便于纸盘调试
        self._balance = Balance(total=1000.0, available=1000.0)
        self._position = Position(symbol=symbol)
        self._orders: dict[str, Order] = {}
        logger.info("PaperBroker 初始化完成: symbol={}", symbol)

    # ------------------------------------------------------------------
    # 行情 / 状态
    # ------------------------------------------------------------------
    async def get_server_time_ms(self) -> int:
        """纸盘模式下直接返回本地时间（毫秒）。"""
        return int(time.time() * 1000)

    async def get_balance(self) -> Balance:
        return self._balance.model_copy(deep=True)

    async def get_position(self, symbol: str) -> Position:
        return self._position.model_copy(deep=True)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return [
            o.model_copy(deep=True)
            for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
        ]

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """纸盘占位：记录订单、默认标记为 PENDING。"""
        cid = client_order_id or self._gen_cid()
        order = Order(
            client_order_id=cid,
            order_id=f"paper-{cid}",
            symbol=symbol,
            side=side,
            type=type,
            price=price,
            amount=amount,
        )
        self._orders[cid] = order
        logger.info("PaperBroker 下单: {}", order.model_dump())
        return order

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        order = self._orders.get(client_order_id)
        if order and order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            order.status = OrderStatus.CANCELED
            order.updated_at = int(time.time() * 1000)
            logger.info("PaperBroker 撤单: {}", client_order_id)
            return True
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _gen_cid() -> str:
        ts = time.strftime("%Y%m%d")
        return f"YL-{ts}-{int(time.time()*1000) % 1_000_000:06d}"
