# -*- coding: utf-8 -*-
"""OKXBroker：通过 ccxt.pro.okx 对接 OKX 永续（设计文档 · 第八节）。

已实现：get_server_time_ms / get_balance / get_position / get_open_orders
待实现（阶段 2）：place_order / cancel_order
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional, Tuple

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
from .base import Balance, Broker, MarketSpec, Order, Position


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
        # 2026-08-30：MarketSpec 缓存（symbol -> (spec, 过期秒时间戳)），避免每次风控都打 OKX
        self._market_spec_cache: dict[str, Tuple[MarketSpec, float]] = {}
        self._market_spec_ttl_s: int = 3600  # 1 小时刷新一次足够（最小下单规则几乎不变）
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

    # 2026-08-31：独立 ticker 价格（解决空仓时 get_position.mark_price=0 → 最小名义卡 2.466U 不跟随现价）。
    #   优先级：ticker.last > mark_price > (bid+ask)/2。全失败才返回 0（交由上层兜底）。
    async def get_ticker_price(self, symbol: Optional[str] = None) -> float:
        """拉取 symbol 的最新成交价 / 标记价。失败返回 0。"""
        key = symbol or self.symbol
        ex = self._ensure_client()
        try:
            t = await ex.fetch_ticker(key)
            if t:
                last = float(t.get("last") or 0.0)
                if last > 0:
                    return last
                mark = float(t.get("info", {}).get("markPx") or 0.0) if isinstance(t.get("info"), dict) else 0.0
                if mark > 0:
                    return mark
                bid = float(t.get("bid") or 0.0)
                ask = float(t.get("ask") or 0.0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
        except Exception:  # noqa: BLE001
            pass
        # 再兜底：走 position 的 mark（若有持仓）
        try:
            p = await self.get_position(key)
            return float(p.mark_price or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------
    # MarketSpec：交易所最小下单 / 面值 / 杠杆上限（2026-08-30 新增）
    # ------------------------------------------------------------------
    @staticmethod
    def _decimals_from_str(s: str, cap: int = 4) -> int:
        """按 lotSz / tickSz 的字符串（0.1, 0.0001, 1）推小数位；最大 cap 位，避免极端精度值撑爆。"""
        try:
            d = Decimal(str(s))
            exponent = d.as_tuple().exponent
            if isinstance(exponent, int):
                return max(0, min(int(-exponent), int(cap)))
        except Exception:  # noqa: BLE001
            pass
        return 0

    async def fetch_market_spec(self, symbol: Optional[str] = None) -> MarketSpec:
        """真实从 OKX 拉交易规则：
            1) 先用 ccxt.load_markets 拿 ctVal / minSz / precision;
            2) 再用 /api/v5/public/instruments 补 minSz / lotSz / maxLever / maxLmtSz / maxMktSz / ctVal；
            3) 全失败就 fallback 默认值（ETH-USDT-SWAP 的公开保守值）。
        结果带 1 小时 TTL 缓存。"""
        key = symbol or self.symbol
        now = time.time()
        cached = self._market_spec_cache.get(key)
        if cached and cached[1] >= now:
            return cached[0]
        ex = self._ensure_client()
        # OKX instId（/api/v5 风格）：ETH-USDT-SWAP；若 ccxt 风格 ETH/USDT:USDT 传入则归一
        inst_id = key if "-SWAP" in key or "-SPOT" in key else key
        # 把 ccxt 格式归一到 instId （简单映射：ETH/USDT:USDT → ETH-USDT-SWAP）
        if ":" in inst_id and "/" in inst_id and "-" not in inst_id:
            base, rest = inst_id.split("/", 1)
            quote = rest.split(":", 1)[0]
            inst_id = f"{base}-{quote}-SWAP"

        collected: dict[str, Any] = {"symbol": key, "source_parts": []}

        # --- L1: ccxt.load_markets（能拿到 precision/limits/contractSize）---
        try:
            markets = await ex.load_markets(reload=False)
            candidates = [key]
            # 把 ccxt 风格符号也加进去互查
            if "-SWAP" in key:
                base2, quote2, _typ = key.split("-", 2)
                candidates.insert(0, f"{base2}/{quote2}:{quote2}")
            m: Any = None
            for cand in candidates:
                m = markets.get(cand)
                if m:
                    break
            if m:
                collected["source_parts"].append("ccxt_load_markets")
                ct_val = float(m.get("contractSize") or 0.01)
                precision = m.get("precision") or {}
                amount_p = precision.get("amount") or m.get("contractSize") or 0.1
                price_p = precision.get("price") or 0.1
                lim = m.get("limits") or {}
                lim_amount = lim.get("amount") or {}
                collected.setdefault("ct_val", ct_val)
                collected.setdefault("lot_sz", float(amount_p))
                collected.setdefault("min_sz", float(lim_amount.get("min") or float(amount_p)))
                collected.setdefault("max_limit_sz", float((lim.get("order") or {}).get("max") or 10_000.0))
                collected.setdefault("max_market_sz", float((lim.get("market") or lim.get("order") or {}).get("max") or 10_000.0))
                collected.setdefault("tick_sz", float(price_p))
        except Exception as e:  # noqa: BLE001
            logger.debug("OKX fetch_market_spec L1(load_markets) 失败，继续：{}", e)

        # --- L2: OKX /api/v5/public/instruments（真官方源）---
        try:
            # ccxt.pro.okx 提供了 unified signed + public API；公共接口直接查
            resp = await ex.public_get_public_instruments(params={
                "instType": "SWAP",
                "instId": inst_id,
            })
            data = ((resp or {}).get("data") or [])
            if not data:
                # 若没有按 instId 命中，再拉全量 SWAP 并按 uly=ETH 过滤（兜底）
                resp2 = await ex.public_get_public_instruments(params={"instType": "SWAP"})
                all_rows = ((resp2 or {}).get("data") or []) if isinstance(resp2, dict) else []
                data = [r for r in all_rows if r.get("instId") == inst_id or r.get("instId") == key]
                if not data:
                    # 再退化：取一个 uly=ETH-USDT / category=linear 的第一个
                    data = [r for r in all_rows if str(r.get("uly", "")).startswith("ETH")]
            if data:
                row = data[0] or {}
                collected["source_parts"].append("okx_public_instruments")

                def _g(fields: list[str], default: Any) -> Any:
                    for fld in fields:
                        v = row.get(fld)
                        if v not in (None, "", "null"):
                            return v
                    return default

                ct_val = float(_g(["ctVal", "contractMultiplier", "ctMult"], collected.get("ct_val") or 0.01))
                min_sz = float(_g(["minSz"], collected.get("min_sz") or 0.1))
                lot_sz = float(_g(["lotSz"], collected.get("lot_sz") or min_sz))
                tick_sz = float(_g(["tickSz"], collected.get("tick_sz") or 0.1))
                max_lmt = float(_g(["maxLmtSz"], collected.get("max_limit_sz") or 10_000.0))
                max_mkt = float(_g(["maxMktSz"], collected.get("max_market_sz") or 10_000.0))
                # 最大杠杆：lever 一般传 "1.25.50.100" 这种段（用最大数兜底）；maxLever（V5 新字段）优先
                lever_raw = _g(["maxLever", "lever"], None)
                max_lever = 125
                if lever_raw is not None:
                    try:
                        s = str(lever_raw)
                        if s.isdigit():
                            max_lever = int(s)
                        else:
                            nums = [int(p) for p in s.replace("x", "").replace("X", "").split(".") if p.isdigit()]
                            if nums:
                                max_lever = max(nums)
                    except Exception:  # noqa: BLE001
                        pass
                # minNotionalUsd（部分品种会直接给；没给就留 0，RiskEngine 会用 minSz×ctVal×entry 推算）
                min_notional = float(_g(["minNotionalUsd", "minVal", "minNominalUsd"], 0.0) or 0.0)
                collected.update({
                    "ct_val": ct_val,
                    "min_sz": min_sz,
                    "lot_sz": lot_sz,
                    "tick_sz": tick_sz,
                    "max_limit_sz": max_lmt,
                    "max_market_sz": max_mkt,
                    "max_lever": int(max_lever),
                    "min_notional_usdt": min_notional,
                })
        except Exception as e:  # noqa: BLE001
            logger.debug("OKX fetch_market_spec L2(public instruments) 失败，继续：{}", e)

        # --- L3: 兜底默认值（ETH-USDT-SWAP）---
        defaults: dict[str, Any] = {
            "ct_val": 0.01,
            "min_sz": 0.1,
            "lot_sz": 0.1,
            "tick_sz": 0.1,
            "max_lever": 125,
            "max_limit_sz": 10_000.0,
            "max_market_sz": 10_000.0,
            "min_notional_usdt": 0.0,
        }
        for k, v in defaults.items():
            collected.setdefault(k, v)
        # 计算 sz_decimals（来源于 lotSz 字符串精度；缺则 1）
        lot_sz_raw: Any = collected.get("lot_sz") or "0.1"
        sz_decimals = self._decimals_from_str(str(lot_sz_raw), cap=4)
        if sz_decimals == 0 and float(lot_sz_raw) < 1:
            sz_decimals = 1  # 兜底

        source = "+".join(collected["source_parts"]) if collected.get("source_parts") else "fallback_defaults"
        spec = MarketSpec(
            symbol=key,
            ct_val=float(collected["ct_val"]),
            min_sz=float(collected["min_sz"]),
            lot_sz=float(collected["lot_sz"]),
            sz_decimals=int(sz_decimals),
            tick_sz=float(collected["tick_sz"]),
            min_notional_usdt=float(collected.get("min_notional_usdt") or 0.0),
            max_lever=int(collected.get("max_lever") or 125),
            max_limit_sz=float(collected.get("max_limit_sz") or 10_000.0),
            max_market_sz=float(collected.get("max_market_sz") or 10_000.0),
            source=source,
        )
        self._market_spec_cache[key] = (spec, now + self._market_spec_ttl_s)
        logger.debug("OKX MarketSpec[{}] OK: source={} minSz={} ctVal={} maxLever={} szDecimals={}",
                     key, source, spec.min_sz, spec.ct_val, spec.max_lever, spec.sz_decimals)
        return spec

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

        # 2026-08-30：对 sz 做交易所 lotSz/szDecimals 规范化（否则 OKX 会报 sz precision / sz lot 错误）
        try:
            spec = await self.fetch_market_spec(symbol)
            norm = spec.clamp_sz(float(amount), is_market=(type == OrderType.MARKET))
            if norm <= 0:
                raise ValueError(f"规范化后 sz=0（原 amount={amount}，spec.minSz={spec.min_sz} lotSz={spec.lot_sz}）")
            if abs(norm - float(amount)) > 1e-9:
                logger.info("OKX place_order sz 规范化：amount={} → {} (lotSz={} decimals={})",
                            amount, norm, spec.lot_sz, spec.sz_decimals)
            amount = norm
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("OKX place_order sz 规范化失败（按原 amount 继续）: {}", e)

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
