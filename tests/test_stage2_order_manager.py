# -*- coding: utf-8 -*-
"""阶段 2-1：OrderManager Maker 挂单 → 20s 轮询 → 超时撤单。

使用 FakeBroker 测试三种情况：
  1) 立即成交 → 返回 FILLED，不会调用 cancel
  2) 一直未成交 → 超时后 Broker.cancel_order 被调用，结果 CANCELED
  3) 重放同 client_order_id → 幂等：不重复下单，直接返回已知订单

同时测试 StateStore.known_ids 幂等持久化。
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from app.broker.base import Broker, Balance, Order, Position
from app.core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SYMBOL,
)
from app.storage.state_store import StateStore
from app.trading.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Fake Broker：可控制订单成交 / 记录 cancel 调用次数
# ---------------------------------------------------------------------------
class FakeControlBroker(Broker):
    """可调的 PaperBroker。

    - 在 submit 后，第 `filled_after_polls` 次 fetch 时返回 FILLED
    - 可通过 cancel_count 读取撤单调用次数
    """

    def __init__(self, filled_after_polls: Optional[int] = None) -> None:
        self.orders: dict[str, Order] = {}
        self.polls: dict[str, int] = {}
        self.cancel_count = 0
        self.place_call_count = 0
        self.filled_after_polls = filled_after_polls  # None = 永远不成交

    async def get_server_time_ms(self) -> int:
        return 0

    async def get_balance(self) -> Balance:
        return Balance()

    async def get_position(self, symbol: str) -> Position:
        return Position(symbol=symbol, side=PositionSide.FLAT)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        # 用于测试用例的 fetch：每调一次 polls+1，到阈值时标为 FILLED
        out = []
        for o in self.orders.values():
            if o.status == OrderStatus.PENDING or o.status == OrderStatus.PARTIAL:
                self.polls[o.client_order_id] = self.polls.get(o.client_order_id, 0) + 1
                n = self.polls[o.client_order_id]
                if self.filled_after_polls is not None and n >= self.filled_after_polls:
                    o.status = OrderStatus.FILLED
                    o.filled = o.amount
                    o.avg_fill_price = o.price or 2000.0
                else:
                    out.append(o.model_copy(deep=True))
        return out

    async def get_order_by_cid(self, symbol: str, cid: str) -> Optional[Order]:
        """为「非 open 订单」提供查询：测试用 OrderManager 恢复 / 幂等场景。"""
        o = self.orders.get(cid)
        return o.model_copy(deep=True) if o else None

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        self.place_call_count += 1
        assert client_order_id, "OrderManager 必须永远传入 client_order_id"
        o = Order(
            client_order_id=client_order_id,
            order_id=f"FAKE-{client_order_id}",
            symbol=symbol,
            side=side,
            type=type,
            price=price,
            amount=amount,
        )
        self.orders[client_order_id] = o
        return o.model_copy(deep=True)

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        self.cancel_count += 1
        o = self.orders.get(client_order_id)
        if o and o.status == OrderStatus.PENDING:
            o.status = OrderStatus.CANCELED
            return True
        return False


# ---------------------------------------------------------------------------
async def _run_with_small_poll_interval(
    om: OrderManager,
    *,
    poll_interval: float = 0.02,
    timeout: int = 1,
) -> Optional[Order]:
    """用小间隔调用 submit_maker_then_cancel，避免测试等 20s。"""
    return await om.submit_maker_then_cancel(
        symbol=SYMBOL,
        side=OrderSide.BUY,
        amount=1,
        price=2000.0,
        timeout=timeout,
        poll_interval=poll_interval,
    )


# ---------------------------------------------------------------------------
def test_maker_timeout_cancels_when_not_filled(tmp_path) -> None:
    """Maker 挂单，始终未成交，超时后调用 cancel，最终状态 CANCELED。"""
    broker = FakeControlBroker(filled_after_polls=None)
    om = OrderManager(broker, state_store=StateStore(tmp_path))
    result = asyncio.run(_run_with_small_poll_interval(om, timeout=1, poll_interval=0.02))
    assert result is not None
    assert result.status == OrderStatus.CANCELED
    assert broker.cancel_count == 1
    assert broker.place_call_count == 1


def test_maker_fills_within_timeout_no_cancel(tmp_path) -> None:
    """第 2 次 fetch 就成交 → 不触发 cancel，返回 FILLED。"""
    broker = FakeControlBroker(filled_after_polls=2)
    om = OrderManager(broker, state_store=StateStore(tmp_path))
    result = asyncio.run(_run_with_small_poll_interval(om, timeout=5, poll_interval=0.02))
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert result.filled == 1.0
    assert broker.cancel_count == 0


def test_maker_idempotent_same_cid_not_resubmitted(tmp_path) -> None:
    """同 client_order_id 两次调用：仅 1 次 place_order。"""
    broker = FakeControlBroker(filled_after_polls=100)  # 始终不成交
    store = StateStore(tmp_path)
    om = OrderManager(broker, state_store=store)

    async def two_submits() -> tuple:
        cid = "YL-20260828-00099"
        a = await om.submit_maker_then_cancel(
            SYMBOL, OrderSide.BUY, 1, price=2000,
            timeout=1, poll_interval=0.02, client_order_id=cid,
        )
        # 第二次：相同 CID
        b = await om.submit_maker_then_cancel(
            SYMBOL, OrderSide.BUY, 1, price=2000,
            timeout=1, poll_interval=0.02, client_order_id=cid,
        )
        return a, b

    a, b = asyncio.run(two_submits())
    # 第一次真实下单 + cancel；第二次幂等，不再次 place_order
    assert broker.place_call_count == 1
    # 但两次都应得到最终订单结果（CANCELED）
    assert a is not None and b is not None
    assert a.client_order_id == b.client_order_id == "YL-20260828-00099"


def test_state_store_persists_known_ids(tmp_path) -> None:
    """known_ids 持久化到 state.json 中，重启 OrderManager 后仍能幂等去重。"""
    store = StateStore(tmp_path)
    broker = FakeControlBroker(filled_after_polls=None)
    om1 = OrderManager(broker, state_store=store)
    asyncio.run(om1.submit_maker_then_cancel(
        SYMBOL, OrderSide.SELL, 1, price=3000,
        timeout=1, poll_interval=0.02,
        client_order_id="YL-20260828-00777",
    ))
    state = store.load()
    assert "YL-20260828-00777" in state["risk"].get("known_order_ids", [])

    # 构造新 OrderManager（模拟重启），再发同 CID → place 不增加
    om2 = OrderManager(broker, state_store=store)
    asyncio.run(om2.submit_maker_then_cancel(
        SYMBOL, OrderSide.SELL, 1, price=3000,
        timeout=1, poll_interval=0.02,
        client_order_id="YL-20260828-00777",
    ))
    assert broker.place_call_count == 1
