# -*- coding: utf-8 -*-
"""订单管理器（设计文档 · 第九节 / 第十二节）。

职责：
  - 统一管理订单生命周期：创建 → 提交 → 轮询 → 成交 / 超时撤单
  - 生成 client_order_id：YL-YYYYMMDD-XXXXX（日内自增）
  - 基于 known_order_ids 做幂等（重复提交同一 CID 不会再向 Broker 下单）
  - Maker 优先：挂 LIMIT 单，等待 N 秒，超时撤单（MAKER_WAIT_TIMEOUT 默认 20s）
"""

from __future__ import annotations

import asyncio
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
from ..storage.state_store import StateStore


_STATE_KNOWN_ORDER_IDS_KEY = ("risk", "known_order_ids")


class OrderManager:
    """订单生命周期管理。"""

    def __init__(
        self,
        broker: Broker,
        *,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self._broker = broker
        self._store = state_store

        # 日内序号，每次 _next_seq() 自增；在 load_from_state 中与 known_order_ids 对齐
        self._seq = self._max_seq_in_known_ids()

    # ------------------------------------------------------------------
    # CID 生成与幂等
    # ------------------------------------------------------------------
    @staticmethod
    def generate_client_order_id(ts: Optional[float] = None, seq: int = 0) -> str:
        """生成幂等订单号：YL-YYYYMMDD-XXXXX。"""
        t = time.localtime(ts) if ts is not None else time.localtime()
        day = time.strftime("%Y%m%d", t)
        return f"{CLIENT_ORDER_PREFIX}-{day}-{seq:05d}"

    def _next_seq(self, ts: Optional[float] = None) -> int:
        self._seq += 1
        return self._seq

    def _known_ids(self) -> set[str]:
        if self._store is None:
            return set()
        st = self._store.load()
        # state.json 默认 schema 中 risk/known_order_ids 不存在，用 setdefault 写入
        st.setdefault("risk", {})
        if "known_order_ids" not in st["risk"]:
            st["risk"]["known_order_ids"] = []
            self._store.save(st)
        return set(st["risk"]["known_order_ids"])

    def _mark_known(self, cid: str) -> None:
        if self._store is None:
            return
        st = self._store.load()
        st.setdefault("risk", {}).setdefault("known_order_ids", [])
        if cid not in st["risk"]["known_order_ids"]:
            st["risk"]["known_order_ids"].append(cid)
            self._store.save(st)

    def _max_seq_in_known_ids(self) -> int:
        """恢复场景：根据 known_order_ids，重算当日最大 seq。"""
        known = self._known_ids()
        today_prefix = f"{CLIENT_ORDER_PREFIX}-{time.strftime('%Y%m%d')}-"
        max_s = 0
        for cid in known:
            if cid.startswith(today_prefix):
                try:
                    s = int(cid[len(today_prefix):])
                    max_s = max(max_s, s)
                except ValueError:
                    pass
        return max_s

    # ------------------------------------------------------------------
    # Maker 限价挂单 → 轮询 → 超时撤单（设计文档 · 第十二节）
    # ------------------------------------------------------------------
    async def submit_maker_then_cancel(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        timeout: int = MAKER_WAIT_TIMEOUT,
        poll_interval: float = 1.0,
        client_order_id: Optional[str] = None,
    ) -> Optional[Order]:
        """Maker First：挂限价单 → 等待 N 秒 → 超时撤单。

        幂等：若 client_order_id 已在 known_order_ids 中，
            跳过 place_order，直接查询 Broker 返回的最新订单状态。

        Args:
            symbol: 交易对
            side: BUY / SELL
            amount: 数量
            price: 限价
            timeout: 等待超时（秒）
            poll_interval: 轮询间隔（秒），单元测试可设小值
            client_order_id: 可选，强制指定；否则按 `YL-YYYYMMDD-XXXXX` 生成

        Returns:
            最终订单（FILLED / PARTIAL / CANCELED）。异常返回 None。
        """
        cid = client_order_id or self.generate_client_order_id(seq=self._next_seq())
        logger.info("OrderManager Maker 挂单: {} side={} price={} amt={} timeout={}s",
                    cid, side.value, price, amount, timeout)

        is_known = cid in self._known_ids()
        order: Optional[Order]

        if is_known:
            # 幂等分支：不重新 place_order，尝试从 Broker 现存订单中找
            logger.info("命中 known_order_ids，跳过 place_order，仅查询状态: {}", cid)
            order = await self._find_order_by_cid(symbol, cid)
        else:
            order = await self._broker.place_order(
                symbol=symbol,
                side=side,
                type=OrderType.LIMIT,
                amount=amount,
                price=price,
                client_order_id=cid,
            )
            self._mark_known(cid)

        deadline = time.monotonic() + timeout
        last_state: Optional[Order] = order
        timed_out = False
        try:
            while True:
                # 每次先主动拉一次 Broker 的订单列表
                open_orders = await self._broker.get_open_orders(symbol)
                match = next((o for o in open_orders if o.client_order_id == cid), None)

                if match is None:
                    # 未在挂单列表 → 已完全成交 / 完全撤销
                    final = await self._find_order_by_cid(symbol, cid)
                    if final is not None:
                        last_state = final
                        break
                    # 兜底：若 filled >= amount 视为 FILLED
                    if last_state and last_state.filled >= max(last_state.amount, 1e-12):
                        last_state.status = OrderStatus.FILLED
                    break

                last_state = match
                if match.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.ERROR):
                    break

                # 是否超时？
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                await asyncio.sleep(poll_interval)

            if timed_out:
                logger.warning("Maker 挂单超时 {}s，撤单: {}", timeout, cid)
                canceled = await self._broker.cancel_order(symbol, cid)
                # 尝试再从 Broker 拿最终状态（含已撤销）
                final_order = await self._find_order_by_cid(symbol, cid)
                if final_order is not None:
                    last_state = final_order
                    if canceled and last_state.status not in (
                        OrderStatus.FILLED, OrderStatus.PARTIAL,
                    ):
                        last_state.status = OrderStatus.CANCELED
                elif last_state is not None:
                    last_state = last_state.model_copy(
                        update={"status": OrderStatus.CANCELED if canceled else last_state.status},
                        deep=True,
                    )
                else:
                    last_state = Order(
                        client_order_id=cid,
                        symbol=symbol,
                        side=side,
                        type=OrderType.LIMIT,
                        price=price,
                        amount=amount,
                        status=OrderStatus.CANCELED,
                    )
        except Exception:
            logger.exception("Maker 轮询异常: cid={}", cid)
            return None

        return last_state

    # ------------------------------------------------------------------
    async def _find_order_by_cid(self, symbol: str, cid: str) -> Optional[Order]:
        """查询指定 client_order_id 的订单，支持「非 open」终态（成交 / 撤销）。

        策略：
          1) 若 Broker 提供 get_order_by_cid（扩展接口，PaperBroker/测试必备），优先用它；
          2) 否则退回 get_open_orders 线性扫描。
        """
        finder = getattr(self._broker, "get_order_by_cid", None)
        if callable(finder):
            try:
                order = await finder(symbol, cid)
                if order is not None:
                    return order
            except TypeError:
                pass
        for o in await self._broker.get_open_orders(symbol):
            if o.client_order_id == cid:
                return o
        return None
