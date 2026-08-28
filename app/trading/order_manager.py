# -*- coding: utf-8 -*-
"""订单管理器（设计文档 · 第九节）。

职责：
  - 统一管理订单生命周期：创建 → 提交 → 等待成交 → 完成
  - 生成 client_order_id：YL-YYYYMMDD-XXXXX
  - 基于 client_order_id 做恢复 / 去重 / 幂等控制
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from ..broker.base import Broker, Order
from ..core.constants import (
    CLIENT_ORDER_PREFIX,
    MAKER_WAIT_TIMEOUT,
    OrderSide,
    OrderStatus,
    OrderType,
)


class OrderManager:
    """订单生命周期管理（占位实现，阶段 2 填充）。"""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._seq = 0  # 日内订单序号，重启后由恢复流程重算

    # ------------------------------------------------------------------
    @staticmethod
    def generate_client_order_id(ts: Optional[float] = None, seq: int = 0) -> str:
        """生成幂等订单号：YL-YYYYMMDD-XXXXX。"""
        t = time.localtime(ts) if ts is not None else time.localtime()
        day = time.strftime("%Y%m%d", t)
        return f"{CLIENT_ORDER_PREFIX}-{day}-{seq:05d}"

    # ------------------------------------------------------------------
    async def submit_maker_then_cancel(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        timeout: int = MAKER_WAIT_TIMEOUT,
    ) -> Optional[Order]:
        """Maker 优先：挂限价单 → 等待 N 秒 → 超时撤单。

        返回最终订单（FILLED / PARTIAL / CANCELED），异常返回 None。
        阶段 2 完整实现。
        """
        self._seq += 1
        cid = self.generate_client_order_id(seq=self._seq)
        order = await self._broker.place_order(
            symbol=symbol,
            side=side,
            type=OrderType.LIMIT,
            amount=amount,
            price=price,
            client_order_id=cid,
        )
        logger.info("OrderManager 挂单 {}，等待 {}s 成交", cid, timeout)
        # 占位：阶段 2 使用 asyncio.sleep + 轮询 Broker 状态
        return order
