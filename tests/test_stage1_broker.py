# -*- coding: utf-8 -*-
"""Broker 阶段 1 测试：PaperBroker 全接口 + OKXBroker 使用 Fake ccxt。

TDD：测试写完后再补实现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import Balance, Order, Position
from app.broker.okx_broker import OKXBroker
from app.broker.paper import PaperBroker
from app.core.config import OKXConfig
from app.core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SYMBOL,
)


# ======================================================================
# PaperBroker：当前持仓、余额、挂单、下单、撤单 完整链路
# ======================================================================
def test_paper_broker_initial_state() -> None:
    b = PaperBroker(symbol=SYMBOL)
    pos = asyncio.run(b.get_position(SYMBOL))
    assert pos.side == PositionSide.FLAT
    assert pos.size == 0.0
    bal = asyncio.run(b.get_balance())
    assert bal.total == 1000.0
    assert bal.available == 1000.0
    assert asyncio.run(b.get_open_orders(SYMBOL)) == []


def test_paper_broker_place_and_cancel_limit_order() -> None:
    """挂限价 LIMIT BUY → open_orders 能看到；撤单 → 返回已撤销。"""
    b = PaperBroker(symbol=SYMBOL)
    cid = "YL-20260828-00001"
    order = asyncio.run(b.place_order(
        symbol=SYMBOL,
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        amount=1,
        price=2000.0,
        client_order_id=cid,
    ))
    assert order.client_order_id == cid
    assert order.side == OrderSide.BUY
    assert order.type == OrderType.LIMIT
    assert order.status == OrderStatus.PENDING
    assert order.price == 2000.0

    opens = asyncio.run(b.get_open_orders(SYMBOL))
    assert len(opens) == 1
    assert opens[0].client_order_id == cid

    ok = asyncio.run(b.cancel_order(SYMBOL, cid))
    assert ok is True
    # 取消后不在 open_orders
    assert asyncio.run(b.get_open_orders(SYMBOL)) == []
    # 幂等：第二次 cancel 返回 False
    assert asyncio.run(b.cancel_order(SYMBOL, cid)) is False


def test_paper_broker_auto_generate_cid_when_missing() -> None:
    """不传入 client_order_id 时也能自动生成且符合前缀。"""
    b = PaperBroker(symbol=SYMBOL)
    o = asyncio.run(b.place_order(
        SYMBOL, OrderSide.SELL, OrderType.MARKET, amount=1,
    ))
    assert o.client_order_id.startswith("YL-")
    assert len(o.client_order_id.split("-")) == 3


# ======================================================================
# OKXBroker + 伪 ccxt（避免真实请求）
# ======================================================================
@dataclass
class _FakeExchange:
    """ccxt okx 伪实现，返回构造好的字典。"""

    balance: dict
    positions: list[dict]
    open_orders: list[dict]
    server_time_ms: int = 1_700_000_000_000

    async def fetch_balance(self, params: Optional[dict] = None) -> dict:
        return self.balance

    async def fetch_positions(self, symbols: Optional[list[str]] = None, params: Optional[dict] = None) -> list[dict]:
        return self.positions

    async def fetch_open_orders(self, symbol: Optional[str] = None, since=None, limit=None, params=None) -> list[dict]:
        return self.open_orders

    async def fetch_time(self) -> int:
        return self.server_time_ms

    async def close(self) -> None:  # pragma: no cover - 无副作用
        return None


def _okx_broker_with_fake(fake: _FakeExchange) -> OKXBroker:
    cfg = OKXConfig(api_key="K", secret="S", passphrase="P")
    broker = OKXBroker(symbol=SYMBOL, okx=cfg)
    # 直接替换内部 exchange
    broker._exchange = fake  # type: ignore[assignment]
    return broker


def test_okx_get_server_time_ms() -> None:
    fake = _FakeExchange(balance={}, positions=[], open_orders=[], server_time_ms=1234567890123)
    brk = _okx_broker_with_fake(fake)
    assert asyncio.run(brk.get_server_time_ms()) == 1234567890123


def test_okx_get_balance_parses_okx_unified_margin() -> None:
    """OKX unified swap 返回结构中 USDT 对应 total / free / used 要正确换算成 Balance。"""
    fake = _FakeExchange(
        balance={
            "USDT": {"total": 1500.0, "free": 1200.0, "used": 300.0},
            "info": {"data": [{"availEq": "1200", "eq": "1500", "upl": "-50"}]},
        },
        positions=[],
        open_orders=[],
    )
    brk = _okx_broker_with_fake(fake)
    bal = asyncio.run(brk.get_balance())
    # total / available 直接读 USDT 总权益 / 可用
    assert bal.total == 1500.0
    assert bal.available == 1200.0
    # info 中的 upl 作为未实现盈亏
    assert bal.unrealized_pnl == pytest.approx(-50.0)


def test_okx_get_position_long_and_flat() -> None:
    """OKX 返回的 long/short/flat 三种情况。"""
    # LONG: contracts = 2, avgPx = 2000, uplRatio = 0.05, lever = 5
    fake = _FakeExchange(
        balance={},
        positions=[{
            "symbol": SYMBOL,
            "side": "long",
            "contracts": 2.0,
            "entryPrice": 2000.0,
            "markPrice": 2100.0,
            "percentage": 50.0,            # ccxt 百分比（相对名义 * 杠杆，直接用 unrealizedPnlRatio）
            "leverage": 5,
            "liquidationPrice": 1800.0,
            "unrealizedPnl": 200.0,
            "info": {"posSide": "long", "pos": "2", "avgPx": "2000", "markPx": "2100", "upl": "200"},
        }],
        open_orders=[],
    )
    brk = _okx_broker_with_fake(fake)
    p = asyncio.run(brk.get_position(SYMBOL))
    assert p.side == PositionSide.LONG
    assert p.size == 2.0
    assert p.entry_price == 2000.0
    assert p.mark_price == 2100.0
    assert p.leverage == 5
    assert p.unrealized_pnl == 200.0
    assert p.liquidation_price == 1800.0

    # FLAT: 空列表 / contracts == 0
    fake_flat = _FakeExchange(balance={}, positions=[], open_orders=[])
    flat = asyncio.run(OKXBroker(symbol=SYMBOL, okx=OKXConfig(api_key="K", secret="S", passphrase="P"))
        .__class__
        .get_position(_okx_broker_with_fake(fake_flat), SYMBOL))
    assert flat.side == PositionSide.FLAT
    assert flat.size == 0.0


def test_okx_get_open_orders_parses() -> None:
    """解析 ccxt fetch_open_orders 响应，映射到统一 Order.status。"""
    orders_resp = [
        {
            "id": "1001",
            "clientOrderId": "YL-20260828-00010",
            "symbol": SYMBOL,
            "side": "buy",
            "type": "limit",
            "price": 2000.0,
            "amount": 1.5,
            "filled": 0.0,
            "average": 0.0,
            "status": "open",
            "timestamp": 1_700_000_000_000,
            "lastUpdateTimestamp": 1_700_000_001_000,
        },
        {
            "id": "1002",
            "clientOrderId": "YL-20260828-00011",
            "symbol": SYMBOL,
            "side": "sell",
            "type": "limit",
            "price": 2200.0,
            "amount": 1.0,
            "filled": 0.4,
            "average": 2200.0,
            "status": "open",
            "timestamp": 1_700_000_100_000,
            "lastUpdateTimestamp": 1_700_000_102_000,
        },
    ]
    fake = _FakeExchange(balance={}, positions=[], open_orders=orders_resp)
    brk = _okx_broker_with_fake(fake)
    orders = asyncio.run(brk.get_open_orders(SYMBOL))
    assert len(orders) == 2
    # 第一张：open & 0 成交 → PENDING
    assert orders[0].order_id == "1001"
    assert orders[0].client_order_id == "YL-20260828-00010"
    assert orders[0].side == OrderSide.BUY
    assert orders[0].type == OrderType.LIMIT
    assert orders[0].status == OrderStatus.PENDING
    # 第二张：部分成交 → PARTIAL
    assert orders[1].client_order_id == "YL-20260828-00011"
    assert orders[1].filled == 0.4
    assert orders[1].status == OrderStatus.PARTIAL
