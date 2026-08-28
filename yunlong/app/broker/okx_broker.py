# -*- coding: utf-8 -*-
"""OKXBroker：通过 ccxt 对接 OKX 实盘（设计文档 · 第八节）。

阶段 1 / 阶段 2 主力实现，当前保留最小骨架。
"""

from __future__ import annotations

from typing import Optional

import ccxt.pro as ccxt_pro
from loguru import logger

from ..core.config import OKXConfig
from ..core.constants import OrderSide, OrderType, SYMBOL
from .base import Balance, Broker, Order, Position


class OKXBroker(Broker):
    """OKX 真实成交 Broker。占位实现，阶段 1 / 2 填充。"""

    def __init__(self, symbol: str = SYMBOL, *, okx: OKXConfig) -> None:
        self.symbol = symbol
        self._cfg = okx
        # 懒初始化：首次使用才连接 OKX，便于配置占位 / 单测
        self._exchange: Optional[ccxt_pro.okx] = None
        logger.info("OKXBroker 初始化完成: symbol={}", symbol)

    # ------------------------------------------------------------------
    def _ensure_client(self) -> ccxt_pro.okx:
        if self._exchange is None:
            self._exchange = ccxt_pro.okx({
                "apiKey": self._cfg.api_key,
                "secret": self._cfg.secret,
                "password": self._cfg.passphrase,
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
        return self._exchange

    # ------------------------------------------------------------------
    async def get_server_time_ms(self) -> int:
        ex = self._ensure_client()
        data = await ex.fetch_time()
        return int(data)

    async def get_balance(self) -> Balance:
        raise NotImplementedError("阶段 1 实现：OKXBroker.get_balance")

    async def get_position(self, symbol: str) -> Position:
        raise NotImplementedError("阶段 1 实现：OKXBroker.get_position")

    async def get_open_orders(self, symbol: str) -> list[Order]:
        raise NotImplementedError("阶段 1 实现：OKXBroker.get_open_orders")

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        raise NotImplementedError("阶段 2 实现：OKXBroker.place_order")

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        raise NotImplementedError("阶段 2 实现：OKXBroker.cancel_order")
