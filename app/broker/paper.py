# -*- coding: utf-8 -*-
"""PaperBroker：本地模拟撮合 Broker（阶段 3 实盘前可用，设计文档 · 第八节）。

功能：
  - 初始余额 / 初始空仓
  - 下单：LIMIT / MARKET / STOP
  - 通过 apply_ticker(last, mark, bid, ask) 驱动撮合
  - LIMIT：若最新价穿过挂单价格 → 按「最新价与挂单价孰优」成交（Maker 对盘撮合理想化）
  - MARKET：下单时立刻按「最新价 + 半个点差」成交
  - STOP：价格穿过 stop_price → 转为市价成交
  - 保证金与仓位更新（支持简单杠杆：默认 3x，简单检查可用余额）
  - 查询：get_order_by_cid 返回终态订单（支持幂等恢复 / OrderManager 轮询）
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from ..core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SYMBOL,
)
from .base import Balance, Broker, Order, Position


class PaperBroker(Broker):
    """本地模拟成交 Broker（可撮合版）。"""

    def __init__(
        self,
        symbol: str = SYMBOL,
        *,
        initial_balance: float = 1000.0,
        leverage: int = 3,
    ) -> None:
        self.symbol = symbol
        self.initial_balance = initial_balance
        self.leverage = leverage

        # 余额 / 持仓 / 订单
        self._balance_total = initial_balance
        self._balance_locked = 0.0     # 挂单锁定保证金
        self._unrealized_pnl = 0.0

        self._position: Position = Position(symbol=symbol, side=PositionSide.FLAT, leverage=leverage)
        self._orders: dict[str, Order] = {}

        # 撮合用：最新 ticker（初始 last=2000 仅用于避免 MARKET 下单除零；首个 apply_ticker 前 LIMIT 单不会「立即成交」）
        self._last: float = 2000.0
        self._mark: float = 2000.0
        self._bid: float = 2000.0
        self._ask: float = 2000.1
        self._ticker_applied: bool = False  # 首个 apply_ticker 之后才认为行情有效

        logger.info("PaperBroker 初始化完成: symbol={} 初始余额={}U 杠杆={}x",
                    symbol, initial_balance, leverage)

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    async def get_server_time_ms(self) -> int:
        return int(time.time() * 1000)

    async def get_balance(self) -> Balance:
        return Balance(
            total=round(self._balance_total + self._unrealized_pnl, 6),
            available=round(self._balance_total - self._balance_locked, 6),
            unrealized_pnl=round(self._unrealized_pnl, 6),
        )

    async def get_position(self, symbol: str) -> Position:
        # 每次调用时根据 mark 重新计算未实现盈亏
        self._refresh_pnl()
        return self._position.model_copy(deep=True)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return [
            o.model_copy(deep=True)
            for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
        ]

    async def get_order_by_cid(self, symbol: str, client_order_id: str) -> Optional[Order]:
        """幂等 / 恢复用：返回任意终态的订单。"""
        o = self._orders.get(client_order_id)
        return o.model_copy(deep=True) if o else None

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
        if amount <= 0:
            raise ValueError("下单数量必须为正")
        cid = client_order_id or self._gen_cid()
        now_ms = int(time.time() * 1000)

        order = Order(
            client_order_id=cid,
            order_id=f"paper-{cid}",
            symbol=symbol,
            side=side,
            type=type,
            price=price,
            amount=amount,
            status=OrderStatus.PENDING,
            created_at=now_ms,
            updated_at=now_ms,
        )
        self._orders[cid] = order

        # 1) MARKET：立即成交
        if type == OrderType.MARKET:
            self._fill_market(order)
        # 2) LIMIT：只有在已经 apply_ticker 初始化行情后才判断即刻可成交，否则一律挂入 PENDING
        elif type == OrderType.LIMIT:
            if self._ticker_applied and self._limit_can_fill_now(side=side, limit_price=price):
                self._fill_order(order, fill_price=self._limit_fill_price(side, price))
            else:
                self._lock_margin(order)
        # 3) STOP：挂入 PENDING，下一次 apply_ticker 触发后转市价成交
        elif type == OrderType.STOP:
            self._lock_margin(order)

        return order.model_copy(deep=True)

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        o = self._orders.get(client_order_id)
        if o is None:
            return False
        if o.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            return False
        o.status = OrderStatus.CANCELED
        o.updated_at = int(time.time() * 1000)
        # 释放保证金锁定
        self._unlock_margin(o)
        logger.info("PaperBroker 撤单: {}", client_order_id)
        return True

    # ------------------------------------------------------------------
    # 撮合驱动：推送最新 ticker
    # ------------------------------------------------------------------
    def apply_ticker(
        self,
        last: float,
        mark: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> None:
        """PaperBroker 外部行情驱动。每收到一个 ticker：

          1) 更新 mark / 未实现浮盈
          2) 扫一遍 PENDING / PARTIAL 的 LIMIT 是否可成交
          3) 扫 STOP 触发并转为市价成交
        """
        self._last = last
        self._mark = mark if mark is not None else last
        spread = max((ask - bid) if (bid is not None and ask is not None) else 0.1, 0.0)
        self._bid = bid if bid is not None else (last - spread / 2)
        self._ask = ask if ask is not None else (last + spread / 2)
        self._ticker_applied = True
        self._refresh_pnl()

        # 快照：遍历
        for cid in list(self._orders.keys()):
            o = self._orders[cid]
            if o.status not in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                continue
            if o.type == OrderType.LIMIT:
                if self._limit_can_fill_now(side=o.side, limit_price=o.price, last=last, bid=self._bid, ask=self._ask):
                    self._fill_order(o, self._limit_fill_price(o.side, o.price, last=last, bid=self._bid, ask=self._ask))
            elif o.type == OrderType.STOP:
                if self._stop_triggered(side=o.side, stop_price=o.price, last=last):
                    # 解冻 STOP 保证金锁定，立即按市价成交
                    self._unlock_margin(o)
                    self._fill_market(o)

    # ------------------------------------------------------------------
    # 内部工具：成交 / 保证金 / 持仓更新
    # ------------------------------------------------------------------
    def _fill_order(self, order: Order, fill_price: float) -> None:
        """把 order 置为 FILLED，更新仓位与已占用保证金。"""
        remaining = order.amount - order.filled
        if remaining <= 0:
            return
        qty = remaining  # PaperBroker 总是一次性全成
        order.filled = order.amount
        order.avg_fill_price = fill_price
        order.status = OrderStatus.FILLED
        order.updated_at = int(time.time() * 1000)

        # 保证金：如被锁（LIMIT/STOP 挂单）则释放
        self._unlock_margin(order)

        # 持仓更新
        self._update_position_on_fill(order.side, qty, fill_price)
        logger.info("PaperBroker 成交: cid={} side={} qty={} @ {} type={}",
                    order.client_order_id, order.side.value, qty, fill_price, order.type.value)

    def _fill_market(self, order: Order) -> None:
        """市价单：对 BUY 用 ask，对 SELL 用 bid。"""
        px = self._ask if order.side == OrderSide.BUY else self._bid
        if px <= 0:
            px = self._last
        self._fill_order(order, fill_price=px)

    # ------------------------------------------------------------------
    def _limit_can_fill_now(
        self,
        *,
        side: OrderSide,
        limit_price: float,
        last: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> bool:
        """判断限价单是否可立即成交（Maker 语义：对盘穿过限价 或 last 穿过限价）。"""
        last = self._last if last is None else last
        bid = self._bid if bid is None else bid
        ask = self._ask if ask is None else ask
        if side == OrderSide.BUY:
            # 买单：对手盘卖价 ≤ limit_price 或 last ≤ limit_price（价格跌穿买单）均视为可成交
            return ask <= limit_price or last <= limit_price
        else:
            # 卖单：对手盘买价 ≥ limit_price 或 last ≥ limit_price（价格涨穿卖单）均视为可成交
            return bid >= limit_price or last >= limit_price

    def _limit_fill_price(
        self,
        side: OrderSide,
        limit_price: float,
        *,
        last: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> float:
        """Maker 成交价格：在「对盘价」「最新价」「限价」之间取对 Maker 最有利的那个。"""
        ask = self._ask if ask is None else ask
        bid = self._bid if bid is None else bid
        last = self._last if last is None else last
        if side == OrderSide.BUY:
            return min(limit_price, ask, last)
        return max(limit_price, bid, last)

    # ------------------------------------------------------------------
    def _stop_triggered(self, *, side: OrderSide, stop_price: float, last: float) -> bool:
        """STOP 是否触发。SELL STOP：last <= stop_price 触发；BUY STOP：last >= stop_price 触发。"""
        if side == OrderSide.SELL:
            return last <= stop_price
        return last >= stop_price

    # ------------------------------------------------------------------
    def _lock_margin(self, order: Order) -> None:
        """挂 LIMIT/STOP 单时，按名义价值 / 杠杆锁住保证金。"""
        notional = (order.price or self._last) * order.amount
        margin = notional / max(self.leverage, 1)
        # 用 broker 内部 dict 追踪 locked 额，不修改 Pydantic Order
        self._margin_locked_map[order.client_order_id] = margin
        self._balance_locked += margin

    def _unlock_margin(self, order: Order) -> None:
        margin = self._margin_locked_map.pop(order.client_order_id, 0.0)
        if margin:
            self._balance_locked = max(0.0, self._balance_locked - margin)

    # 用普通 dict 存锁定金额，避免污染 Pydantic model
    @property
    def _margin_locked_map(self) -> dict:
        if not hasattr(self, "__margin_locked_map"):
            self.__margin_locked_map: dict = {}
        return self.__margin_locked_map

    # ------------------------------------------------------------------
    def _update_position_on_fill(self, side: OrderSide, qty: float, price: float) -> None:
        """根据订单方向 & 数量，更新持仓（V1 单仓位，开 / 平两种路径）。"""
        cur = self._position
        # 把订单方向映射到对持仓的「增减」方向
        # BUY 开多 / SELL 平空 → 增加 LONG / 减少 SHORT
        # SELL 开空 / BUY 平多 → 增加 SHORT / 减少 LONG
        opens_long = side == OrderSide.BUY and cur.side in (PositionSide.FLAT, PositionSide.LONG)
        closes_long = side == OrderSide.SELL and cur.side == PositionSide.LONG
        closes_short = side == OrderSide.BUY and cur.side == PositionSide.SHORT
        opens_short = side == OrderSide.SELL and cur.side in (PositionSide.FLAT, PositionSide.SHORT)

        if opens_long:
            self._open_or_add(PositionSide.LONG, qty, price)
        elif closes_long:
            self._close_or_reduce(PositionSide.LONG, qty, price)
        elif closes_short:
            self._close_or_reduce(PositionSide.SHORT, qty, price)
        elif opens_short:
            self._open_or_add(PositionSide.SHORT, qty, price)
        else:
            # 反向开仓（先平再开）—— V1 禁止双向持仓，所以默认等价于「先平后开」
            self._close_or_reduce(cur.side, qty, price)
            new_side = PositionSide.LONG if side == OrderSide.BUY else PositionSide.SHORT
            self._open_or_add(new_side, 0.0, price)  # 已平完，按当前 size=0 不追加，下面实际为 0

    def _open_or_add(self, side: PositionSide, qty: float, price: float) -> None:
        if self._position.side == PositionSide.FLAT:
            self._position.side = side
            self._position.size = qty
            self._position.entry_price = price
        elif self._position.side == side:
            # 简单加权均价
            total_qty = self._position.size + qty
            if total_qty > 0:
                self._position.entry_price = (
                    (self._position.entry_price * self._position.size + price * qty) / total_qty
                )
            self._position.size = total_qty
        else:  # 不支持加仓反向
            raise RuntimeError("V1 单仓位，禁止双向持仓时的加仓反向")

    def _close_or_reduce(self, side: PositionSide, qty: float, price: float) -> None:
        if self._position.side != side:
            return
        reduce = min(qty, self._position.size)
        self._position.size -= reduce
        # 已实现盈亏从「总权益」里出：余额 += realized_pnl
        if side == PositionSide.LONG:
            realized = (price - self._position.entry_price) * reduce
        else:
            realized = (self._position.entry_price - price) * reduce
        self._balance_total += realized
        if self._position.size <= 0:
            self._position = Position(symbol=self.symbol, side=PositionSide.FLAT, leverage=self.leverage)
        logger.info("PaperBroker 平仓/减仓: side={} reduce={} @{} 已实现盈亏={}",
                    side.value, reduce, price, round(realized, 6))

    # ------------------------------------------------------------------
    def _refresh_pnl(self) -> None:
        """根据 mark / 持仓重新计算未实现盈亏 & 标记价格。"""
        if self._position.side == PositionSide.FLAT:
            self._unrealized_pnl = 0.0
            self._position.mark_price = self._mark
            self._position.unrealized_pnl = 0.0
            return
        self._position.mark_price = self._mark
        if self._position.side == PositionSide.LONG:
            upl = (self._mark - self._position.entry_price) * self._position.size
        else:
            upl = (self._position.entry_price - self._mark) * self._position.size
        self._unrealized_pnl = upl
        self._position.unrealized_pnl = upl

    # ------------------------------------------------------------------
    @staticmethod
    def _gen_cid() -> str:
        ts = time.strftime("%Y%m%d")
        return f"YL-{ts}-{int(time.time()*1000) % 1_000_000:06d}"
