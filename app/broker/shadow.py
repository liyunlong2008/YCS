# -*- coding: utf-8 -*-
"""A7. ShadowBroker 影子模式包装器（护栏 A7 · 只记日志不真发）。

当 risk_limits.shadow_mode=True 时，factory.build_broker 返回 ShadowBroker(inner)：
  · get_balance / get_position / get_open_orders / get_server_time_ms → 100% 透传 inner
    （保证行情/余额/持仓的链路观察是真实的）
  · place_order / cancel_order → 绝不调用 inner，直接返回"假成功"结果：
      - place_order：立即 FILLED，filled=amount，avg_fill_price 使用传入的 price
        （市价单没传 price 时用 last price/0，调用方会做"市价单不校验 price"的处理）
      - cancel_order：永远返回 True
  · 另外把"影子订单"用 loguru.logger 打出来，方便 journal_ext / 日志 回看链路。

双保险：
  1) ShadowBroker 包装层（factory 层保证一进入 shadow 就被套上）
  2) OKXBroker.place_order 首行还有 should_block_real_orders 闸门兜底（防止有人
     绕过 factory 直接 new OKXBroker + 开启 shadow 时真发单）。
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from ..core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    SYMBOL,
)
from .base import Balance, Broker, Order, Position


_SHADOW_ORDER_ID_PREFIX = "YCS-SHADOW-"


class ShadowBroker(Broker):
    """影子 Broker：把 write 路径拦截成"假成功"，read 路径 100% 透传。"""

    def __init__(self, inner: Broker, *, symbol: str = SYMBOL) -> None:
        self._inner = inner
        self.symbol = symbol
        logger.warning(
            "[SHADOW MODE] ShadowBroker 已启用：place_order/cancel_order 将不会真正发送给交易所，"
            "仅打印日志并返回模拟成功结果。inner_broker={}",
            type(inner).__name__,
        )

    # ---- read 路径：100% 透传 inner ----
    async def get_server_time_ms(self) -> int:
        return await self._inner.get_server_time_ms()

    async def get_balance(self) -> Balance:
        return await self._inner.get_balance()

    async def get_position(self, symbol: str) -> Position:
        return await self._inner.get_position(symbol)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return await self._inner.get_open_orders(symbol)

    async def get_order_by_cid(self, symbol: str, client_order_id: str) -> Optional[Order]:
        # Shadow 本地只返回被自己"影子成交"过的订单：这里不伪造本地记录，
        # 直接透传 inner（真交易所不会有这条单，返回 None，符合预期）。
        if hasattr(self._inner, "get_order_by_cid"):
            return await self._inner.get_order_by_cid(symbol, client_order_id)
        return None

    # ---- write 路径：拦截 & 返回假成功 ----
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        ts = int(time.time() * 1000)
        cid = client_order_id or f"{_SHADOW_ORDER_ID_PREFIX}{ts}-{id(self) & 0xffff:04x}"
        # 影子：假设全量 100% 成交
        filled = float(amount)
        avg_fill_price = float(price) if price and type != OrderType.MARKET else (
            float(price) if price else 0.0
        )
        order = Order(
            client_order_id=cid,
            order_id=cid,  # shadow: 无需真 exchange id
            symbol=symbol,
            side=side,
            type=type,
            price=float(price),
            amount=float(amount),
            filled=filled,
            avg_fill_price=avg_fill_price,
            status=OrderStatus.FILLED,
            created_at=ts,
            updated_at=ts,
        )
        logger.warning(
            "[SHADOW MODE] 拦截真实下单 → 已按 FILLED 伪造成交："
            "symbol={symbol} side={side} type={type} amount={amount} price={price} "
            "avg_fill={avg} client_order_id={cid}",
            symbol=symbol, side=side.value, type=type.value,
            amount=amount, price=price, avg=avg_fill_price, cid=cid,
        )
        return order

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        logger.warning(
            "[SHADOW MODE] 拦截真实撤单 → 返回 True（伪造撤单成功）："
            "symbol={} client_order_id={}",
            symbol, client_order_id,
        )
        return True

    # ---- 可选：PaperBroker 专属 apply_ticker 等需要透传（否则 paper 模式下也套着会丢行情）----
    def __getattr__(self, item: str):
        # 未显式实现的属性/方法，都交给 inner：保证 apply_ticker / 额外扩展点不丢。
        return getattr(self._inner, item)
