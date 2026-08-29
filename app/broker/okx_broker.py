# -*- coding: utf-8 -*-
"""OKXBroker：通过 ccxt.pro.okx 对接 OKX 永续（设计文档 · 第八节）。

已实现：get_server_time_ms / get_balance / get_position / get_open_orders
待实现（阶段 2）：place_order / cancel_order
"""

from __future__ import annotations

import time
from typing import Any, Optional

import ccxt.pro as ccxt_pro
from loguru import logger

from ..core.config import OKXConfig
from ..core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SYMBOL,
)
from ..core.safety import should_block_real_orders
from .base import Balance, Broker, Order, Position


_CCXT_SIDE_TO_LOCAL = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}
_CCXT_TYPE_TO_LOCAL = {"limit": OrderType.LIMIT, "market": OrderType.MARKET, "stop": OrderType.STOP}
_CCXT_ORDER_STATUS_TO_LOCAL = {
    "open": OrderStatus.PENDING,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.ERROR,
}


def _map_order_status(status: str, filled: float, amount: float) -> OrderStatus:
    """ccxt status + filled/amount → 本地 OrderStatus（含 PARTIAL）。"""
    base = _CCXT_ORDER_STATUS_TO_LOCAL.get(status)
    if base is None:
        # 默认保守处理
        return OrderStatus.ERROR
    if base == OrderStatus.PENDING and filled > 0 and filled < max(amount, 1e-12):
        return OrderStatus.PARTIAL
    # ccxt 有时 open+部分成交 仍标 open：若 filled == amount 视为已成交
    if amount > 0 and filled >= amount:
        return OrderStatus.FILLED
    return base


def _map_position_side(side: Optional[str], contracts: float) -> PositionSide:
    """ccxt side + 合约数 → 本地方向。"""
    if not contracts or contracts == 0:
        return PositionSide.FLAT
    s = (side or "").lower()
    if s in ("long", "buy"):
        return PositionSide.LONG
    if s in ("short", "sell"):
        return PositionSide.SHORT
    # 双向持仓模式下 ccxt 会明确标出 long/short；若缺但数量非 0，通过 contracts 正负推断
    if contracts > 0:
        return PositionSide.LONG
    return PositionSide.SHORT


class OKXBroker(Broker):
    """OKX 永续合约 Broker。通过 ccxt.pro.okx 异步访问。

    注意：影子模式下 ShadowBroker 已在 factory 层拦截写路径。
    这里额外在 place_order/cancel_order 第一行放 should_block_real_orders 闸门
    （双保险：防止有人绕过 factory 直接 new OKXBroker + 配置 shadow_mode 时误发单）。
    """

    # 允许运行时通过实例属性覆盖 shadow_mode 判断（默认 False，由 outer 包装器保证）。
    # 真的要让 OKXBroker 自己也能拦：就从 config 注入——这里提供 setter 方便。
    def __init__(
        self,
        symbol: str = SYMBOL,
        *,
        okx: OKXConfig,
        shadow_mode: bool = False,
    ) -> None:
        self.symbol = symbol
        self._cfg = okx
        self._shadow_mode = bool(shadow_mode)
        # 懒初始化：便于测试时注入 Fake exchange
        self._exchange: Optional[ccxt_pro.okx] = None
        logger.info("OKXBroker 初始化完成: symbol={} shadow={}", symbol, self._shadow_mode)

    def set_shadow_mode(self, value: bool) -> None:
        """实盘前可随时切：True=闸门拦截所有下单/撤单。"""
        self._shadow_mode = bool(value)

    # ------------------------------------------------------------------
    # 内部：确保 ccxt 客户端
    # ------------------------------------------------------------------
    def _ensure_client(self) -> ccxt_pro.okx:
        if self._exchange is None:
            self._exchange = ccxt_pro.okx({
                "apiKey": self._cfg.api_key,
                "secret": self._cfg.secret,
                "password": self._cfg.passphrase,
                "options": {
                    "defaultType": "swap",           # 永续合约
                    "defaultSubaccount": None,
                },
                "enableRateLimit": True,
                "headers": {"content-type": "application/json"},
            })
        return self._exchange

    # ------------------------------------------------------------------
    # 行情 / 状态
    # ------------------------------------------------------------------
    async def get_server_time_ms(self) -> int:
        """获取 OKX 服务器时间（毫秒）。"""
        ex = self._ensure_client()
        ts = await ex.fetch_time()
        # ccxt.pro.okx.fetch_time 返回毫秒级 unix 时间戳（int/float）
        return int(ts)

    async def get_balance(self) -> Balance:
        """查询 OKX 账户余额（USDT）。

        优先级：
          1. 统一响应中的 USDT 维度（total/free/used）
          2. OKX info.data[0]（availEq / eq / upl）作为兜底 / 未实现盈亏来源
        """
        ex = self._ensure_client()
        raw = await ex.fetch_balance(params={"instType": "MARGIN"})
        usdt = (raw or {}).get("USDT") or {}

        total = float(usdt.get("total", 0.0) or 0.0)
        available = float(usdt.get("free", 0.0) or 0.0)
        unrealized = 0.0

        info_data = ((raw or {}).get("info") or {}).get("data") or []
        if info_data:
            first = info_data[0] or {}
            # 若 unified 响应没有 total/free（OKX 偶发未含），走 info 字段
            if total == 0:
                total = float(first.get("eq") or 0)
            if available == 0:
                available = float(first.get("availEq") or first.get("availBal") or 0)
            unrealized = float(first.get("upl") or first.get("unrealizedPnl") or 0)

        return Balance(
            total=total,
            available=available,
            unrealized_pnl=unrealized,
        )

    async def get_position(self, symbol: str) -> Position:
        """查询 OKX 当前持仓（ETH-USDT-SWAP，单仓位）。"""
        ex = self._ensure_client()
        positions = await ex.fetch_positions(
            symbols=[symbol],
            params={"instType": "SWAP"},
        )
        # 过滤空仓位（OKX 可能返回多条但数量为 0）
        candidates = [p for p in (positions or []) if float(p.get("contracts") or p.get("amount") or 0) != 0]
        if not candidates:
            return Position(symbol=symbol, side=PositionSide.FLAT)
        # V1 单仓位：取第一个
        p = candidates[0]
        contracts = float(p.get("contracts") or p.get("amount") or 0)
        side = _map_position_side(p.get("side"), contracts)
        leverage = int(float(p.get("leverage") or 1))
        upl = float(p.get("unrealizedPnl") or (p.get("info") or {}).get("upl") or 0)
        liq = float(p.get("liquidationPrice") or 0)
        return Position(
            symbol=symbol,
            side=side,
            size=abs(contracts),
            entry_price=float(p.get("entryPrice") or p.get("average") or 0),
            mark_price=float(p.get("markPrice") or 0),
            unrealized_pnl=upl,
            leverage=leverage,
            liquidation_price=liq,
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """查询 OKX 未成交挂单。"""
        ex = self._ensure_client()
        raw_orders = await ex.fetch_open_orders(symbol=symbol)
        out: list[Order] = []
        for o in raw_orders or []:
            amount = float(o.get("amount") or 0)
            filled = float(o.get("filled") or 0)
            out.append(Order(
                client_order_id=o.get("clientOrderId") or "",
                order_id=str(o.get("id") or ""),
                symbol=o.get("symbol") or symbol,
                side=_CCXT_SIDE_TO_LOCAL.get(o.get("side"), OrderSide.BUY),
                type=_CCXT_TYPE_TO_LOCAL.get(o.get("type"), OrderType.LIMIT),
                price=float(o.get("price") or 0),
                amount=amount,
                filled=filled,
                avg_fill_price=float(o.get("average") or 0),
                status=_map_order_status(str(o.get("status") or ""), filled, amount),
                created_at=int(o.get("timestamp") or 0),
                updated_at=int(o.get("lastUpdateTimestamp") or o.get("timestamp") or 0),
            ))
        return out

    # ------------------------------------------------------------------
    # 交易（阶段 2 实现 · 带 A7 影子闸门双保险）
    # ------------------------------------------------------------------
    def _shadow_filled_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float,
        client_order_id: Optional[str],
    ) -> Order:
        """影子闸门触发时：返回一张"影子 FILLED 订单"（与 ShadowBroker 语义一致）。"""
        ts = int(time.time() * 1000)
        cid = client_order_id or f"YCS-SHADOW-OKX-{ts}-{id(self) & 0xffff:04x}"
        avg_fill = float(price) if price and type != OrderType.MARKET else (
            float(price) if price else 0.0
        )
        logger.warning(
            "[SHADOW GATE OKXBroker] shadow_mode=True → 拦截真实下单："
            "symbol={} side={} type={} amount={} price={} cid={}",
            symbol, side.value, type.value, amount, price, cid,
        )
        return Order(
            client_order_id=cid,
            order_id=cid,
            symbol=symbol,
            side=side,
            type=type,
            price=float(price),
            amount=float(amount),
            filled=float(amount),
            avg_fill_price=avg_fill,
            status=OrderStatus.FILLED,
            created_at=ts,
            updated_at=ts,
        )

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        # A7 双保险：即使外层 ShadowBroker 被绕过，这里也不会真发单
        if should_block_real_orders(shadow_mode=self._shadow_mode):
            return self._shadow_filled_order(symbol, side, type, amount, price, client_order_id)
        if amount <= 0:
            raise ValueError("下单数量必须为正")

        ex = self._ensure_client()
        side_str = "buy" if side == OrderSide.BUY else "sell"
        type_str = {
            OrderType.LIMIT: "limit",
            OrderType.MARKET: "market",
            OrderType.STOP: "stop",
        }.get(type, "limit")

        params: dict[str, Any] = {}  # type: ignore[name-defined]  # Any 下面再导入
        if client_order_id:
            params["clientOrderId"] = client_order_id
        # OKX ccxt：限价单传 price；市价单不传/可为 0
        kwargs: dict[str, Any] = {}
        if type == OrderType.LIMIT and price:
            kwargs["price"] = float(price)
        if params:
            kwargs["params"] = params

        try:
            raw = await ex.create_order(
                symbol=symbol,
                type=type_str,
                side=side_str,
                amount=float(amount),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("OKX place_order 失败: symbol={} side={} type={} amount={} err={}",
                         symbol, side.value, type.value, amount, exc)
            ts = int(time.time() * 1000)
            return Order(
                client_order_id=client_order_id or "",
                order_id="",
                symbol=symbol,
                side=side,
                type=type,
                price=float(price),
                amount=float(amount),
                filled=0.0,
                avg_fill_price=0.0,
                status=OrderStatus.ERROR,
                created_at=ts,
                updated_at=ts,
            )
        raw = raw or {}
        _amt = float(raw.get("amount") or raw.get("filled") or amount)
        _filled = float(raw.get("filled") or 0)
        return Order(
            client_order_id=str(raw.get("clientOrderId") or client_order_id or ""),
            order_id=str(raw.get("id") or ""),
            symbol=raw.get("symbol") or symbol,
            side=_CCXT_SIDE_TO_LOCAL.get(str(raw.get("side")), side),
            type=_CCXT_TYPE_TO_LOCAL.get(str(raw.get("type")), type),
            price=float(raw.get("price") or price or 0),
            amount=_amt,
            filled=_filled,
            avg_fill_price=float(raw.get("average") or 0),
            status=_map_order_status(
                str(raw.get("status") or ""),
                _filled,
                _amt,
            ),
            created_at=int(raw.get("timestamp") or time.time() * 1000),
            updated_at=int(raw.get("lastUpdateTimestamp") or raw.get("timestamp") or time.time() * 1000),
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        # A7 双保险：影子模式直接返回 True（不真发撤单）
        if should_block_real_orders(shadow_mode=self._shadow_mode):
            logger.warning("[SHADOW GATE OKXBroker] shadow_mode=True → 拦截真实撤单 cid={}",
                           client_order_id)
            return True
        if not client_order_id:
            return False
        ex = self._ensure_client()
        try:
            await ex.cancel_order(id=None, symbol=symbol, params={"clientOrderId": client_order_id})
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("OKX cancel_order 失败: cid={} err={}", client_order_id, exc)
            return False
