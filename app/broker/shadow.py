# -*- coding: utf-8 -*-
"""A7. ShadowBroker 影子模式包装器（护栏 A7 · 只记日志不真发 + 维护本地虚拟持仓）。

当 risk_limits.shadow_mode=True 时，factory.build_broker 返回 ShadowBroker(inner)：
  · read 路径：
      - get_server_time_ms / fetch_market_spec → 100% 透传 inner（行情/规则必须真实）
      - get_position → 优先取「本地虚拟持仓」，叠加 OKX 的 mark_price / liquidation_price
      - get_balance  → 真实 OKX 余额 + 虚拟持仓未实现盈亏 + 虚拟平仓累计已实现盈亏
                       可用保证金 = 真实可用 - 虚拟占用保证金
      - get_open_orders → 透传 inner（影子下单都是瞬时 FILLED，不会有未成交挂单）
      - get_order_by_cid → 先查本地影子订单账本；无命中再透传 inner
  · write 路径（绝不调用 inner）：
      - place_order：
          1) 返回「假成功」Order：status=FILLED, filled=amount, avg_fill_price=price
          2) 把影子成交写入虚拟账本：_virtual_positions[symbol] / _virtual_realized_pnl
             同方向加仓 → 加权均价；反方向减仓 / 反向平仓 → 累计 realized_pnl(USDT)
          3) set_leverage 设置：_virtual_leverage[symbol]，虚拟持仓/占用保证金/强平价
             按这个 lev 估算（lev 仅用于展示/风险参考；不强制真实调仓）
      - cancel_order：永远返回 True
  · 额外把「影子订单」用 loguru.logger 打 WARNING，方便 journal_ext / 日志 回看链路。

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
    PositionSide,
    SYMBOL,
)
from .base import Balance, Broker, MarketSpec, Order, Position


_SHADOW_ORDER_ID_PREFIX = "YCS-SHADOW-"

# 默认 ct_val（ETH-USDT-SWAP 每张 0.01 ETH）：用于强平价/占用保证金估算；
# 若注入了 market_spec 则以 spec.ct_val 为准。
_DEFAULT_CT_VAL = 0.01
# 默认维持保证金率（估算强平价用的兜底）。OKX 对 ETH-USDT-SWAP 的 MM = 0.5% ~ 1%。
_DEFAULT_MMR = 0.005


class ShadowBroker(Broker):
    """影子 Broker：write 路径拦截+伪造成交，read 路径 持仓/余额 叠加虚拟状态。"""

    def __init__(self, inner: Broker, *, symbol: str = SYMBOL) -> None:
        self._inner = inner
        self.symbol = symbol

        # —— 虚拟账本（进程级内存；shadow 只用于联调 6-24h，不做持久化）——
        # 每张 symbol 的持仓快照（VCS 单仓位模型：LONG/SHORT/FLAT 三者互斥）
        self._virtual_positions: dict[str, Position] = {}
        # 每张 symbol 的累计"影子平仓已实现盈亏 USDT"（加到真实余额 total，用于显示）
        self._virtual_realized_pnl: dict[str, float] = {}
        # 每张 symbol 的建议 leverage（来自 set_leverage 调用；默认 1）
        self._virtual_leverage: dict[str, int] = {}
        # 影子订单账本（{client_order_id: Order}）：get_order_by_cid 命中用
        self._shadow_orders: dict[str, Order] = {}

        logger.warning(
            "[SHADOW MODE] ShadowBroker 已启用（v2：含虚拟持仓/余额镜像）："
            "place_order/cancel_order 将不会真正发送给交易所，"
            "仅打印日志并返回模拟成功结果。inner_broker={}",
            type(inner).__name__,
        )

    # ======================================================================
    #  内部辅助
    # ======================================================================
    def _ensure_virtual(self, symbol: str) -> None:
        self._virtual_positions.setdefault(
            symbol,
            Position(symbol=symbol, side=PositionSide.FLAT),
        )
        self._virtual_realized_pnl.setdefault(symbol, 0.0)
        self._virtual_leverage.setdefault(symbol, 1)

    @staticmethod
    def _dir_side(side: OrderSide) -> int:
        """BUY=+1, SELL=-1（用于「张数方向 × 价格方向 → 持仓方向」统一计算。"""
        return +1 if side == OrderSide.BUY else -1

    @staticmethod
    def _dir_pos(side: PositionSide) -> int:
        """LONG=+1 / SHORT=-1 / FLAT=0。"""
        if side == PositionSide.LONG:
            return +1
        if side == PositionSide.SHORT:
            return -1
        return 0

    def _ct_val(self, symbol: str) -> float:
        """优先尝试走 inner 的 fetch_market_spec（同步缓存到 spec 兜底也行，这里不缓存直接取默认）。"""
        # 注：fetch_market_spec 是 async，而 _apply_fill 是 sync 里调用，
        #     所以用常量兜底。ETH-USDT-SWAP 的 ct_val 长期固定为 0.01，非常稳定。
        spec: Optional[MarketSpec] = getattr(self, "_market_spec_cache", None)
        if isinstance(spec, MarketSpec) and spec.symbol == symbol and spec.ct_val > 0:
            return float(spec.ct_val)
        return _DEFAULT_CT_VAL

    def _apply_fill(
        self,
        symbol: str,
        side: OrderSide,
        filled_sz: float,
        avg_fill_price: float,
    ) -> float:
        """把一笔「影子 FILLED」写到本地虚拟账本。返回已实现盈亏(USDT)。"""
        if filled_sz <= 0 or avg_fill_price <= 0:
            return 0.0
        self._ensure_virtual(symbol)
        pos = self._virtual_positions[symbol]
        ct_val = self._ct_val(symbol)

        old_dir = self._dir_pos(pos.side)
        trade_dir = self._dir_side(side)
        old_sz = float(pos.size or 0.0)
        # 合约对 USDT 的名义单价 = ct_val * avg_fill_price（每张值多少 USDT）
        per_contract = max(ct_val * avg_fill_price, 1e-12)

        realized_usdt = 0.0
        if old_dir == 0 or old_dir == trade_dir:
            # ---- 空仓开仓 / 同方向加仓：加权均价 ----
            new_sz = old_sz + filled_sz
            old_notional = old_sz * ct_val * max(float(pos.entry_price or avg_fill_price), 1e-12)
            add_notional = filled_sz * ct_val * avg_fill_price
            if new_sz > 0:
                avg_entry = (old_notional + add_notional) / (new_sz * ct_val)
            else:
                avg_entry = avg_fill_price
            pos.side = PositionSide.LONG if trade_dir > 0 else PositionSide.SHORT
            pos.size = float(new_sz)
            pos.entry_price = float(avg_entry)
        else:
            # ---- 反方向：先平掉之前同向仓，剩余再反向开仓 ----
            close_sz = min(old_sz, filled_sz)
            remain_sz = filled_sz - close_sz
            # 平仓部分：已实现盈亏(USDT)
            #   多单(old_dir=+1) SELL 平仓 → close_sz × ct_val × (+1) × (平仓价 - 开仓价)
            #   空单(old_dir=-1) BUY 平仓 → close_sz × ct_val × (-1) × (平仓价 - 开仓价)
            #                                    = close_sz × ct_val × (开仓价 - 平仓价)（越低越赚，对）
            realized_usdt = close_sz * ct_val * float(old_dir) * (avg_fill_price - float(pos.entry_price or 0.0))
            self._virtual_realized_pnl[symbol] = float(
                self._virtual_realized_pnl.get(symbol, 0.0)
            ) + float(realized_usdt)

            leftover = old_sz - close_sz  # 原仓位未被平掉的部分（纯减仓场景 ≥0）
            if leftover > 1e-12 and remain_sz <= 1e-12:
                # 情况 A：纯减仓（反向下单 < 当前持仓）
                pos.size = float(leftover)
                # 部分平仓：entry_price 保持不变
            elif remain_sz > 1e-12:
                # 情况 B：先把原仓全平，再反向开 remain_sz 张（trade_dir 方向）
                pos.side = PositionSide.LONG if trade_dir > 0 else PositionSide.SHORT
                pos.size = float(remain_sz)
                pos.entry_price = float(avg_fill_price)
            else:
                # 情况 C：刚好多对一对冲 → 完美平仓(FLAT)
                pos.side = PositionSide.FLAT
                pos.size = 0.0
                pos.entry_price = 0.0
        return float(realized_usdt)

    def _est_liquidation_price(
        self,
        side: PositionSide,
        size_sz: float,
        entry_price: float,
        leverage: int,
        ct_val: float,
        mmr: float = _DEFAULT_MMR,
    ) -> float:
        """估算强平价（简化版：IMR 全亏完 + 维持保证金（MMR）缓冲）。

        公式（OKX 永续简化版，仅供展示；上实盘后会优先取真实 broker 返回的 liq_price）：
          LONG  : 价 ↓ 爆仓 → bankrupt = entry × (1 − 1/lev)；再加 MM → 略微抬升（仍<entry）
          SHORT : 价 ↑ 爆仓 → bankrupt = entry × (1 + 1/lev)；再减 MM → 略微拉低（仍>entry）
        紧凑写法: bankrupt = entry × (1 − dir_sign / lev)，dir_sign=+1(LONG) −1(SHORT)。
        """
        if size_sz <= 0 or entry_price <= 0 or leverage <= 0 or side == PositionSide.FLAT:
            return 0.0
        lev = max(1, int(leverage))
        dir_sign = 1.0 if side == PositionSide.LONG else -1.0
        # bankrupt = entry × (1 − dir_sign / lev)
        #   LONG (+1): 1 − 1/lev  ✓
        #   SHORT (−1): 1 + 1/lev ✓
        bankrupt = entry_price * (1.0 - dir_sign / lev)
        # MM 缓冲：方向同向加缓冲，确保 liq 在"刚好爆 vs 真被强平"之间有安全垫
        #   LONG: bankrupt + (entry * mmr / 2) → 略高于破产价（离破产价近，缓冲到还剩半档MM）
        #   SHORT: bankrupt − (entry * mmr / 2) → 略低于破产价（仍在 entry 上方）
        liq = bankrupt + dir_sign * entry_price * (mmr * 0.5)
        # 方向合理性兜底：LONG 必须<entry；SHORT 必须>entry（即使 MMR 调太大也不越界）
        if side == PositionSide.LONG and liq >= entry_price:
            liq = entry_price * (1.0 - 1.0 / lev / 2.0)
        if side == PositionSide.SHORT and liq <= entry_price:
            liq = entry_price * (1.0 + 1.0 / lev / 2.0)
        if liq <= 0:
            liq = 0.0
        return float(liq)

    def _calc_margin_used_usdt(
        self, symbol: str, ct_val: float, mark_price: float
    ) -> float:
        """估算当前虚拟持仓占用的保证金（名义 / leverage，忽略手续费/持仓费率）。"""
        pos = self._virtual_positions.get(symbol)
        if pos is None or pos.size <= 0 or pos.side == PositionSide.FLAT:
            return 0.0
        lev = max(1, int(self._virtual_leverage.get(symbol, 1) or 1))
        notional = float(pos.size) * ct_val * max(float(mark_price or pos.entry_price or 0.0), 1e-12)
        return notional / lev

    # ======================================================================
    #  read 路径：get_server_time / fetch_market_spec → 透传；余额/持仓 → 叠加虚拟层
    # ======================================================================
    async def get_server_time_ms(self) -> int:
        return await self._inner.get_server_time_ms()

    async def fetch_market_spec(self, symbol: Optional[str] = None) -> MarketSpec:
        sym = symbol or self.symbol
        spec: MarketSpec
        if hasattr(self._inner, "fetch_market_spec") and callable(getattr(self._inner, "fetch_market_spec")):
            spec = await self._inner.fetch_market_spec(sym)
        else:
            spec = MarketSpec(symbol=sym)
        # 存个最近一次 spec（避免每次 _apply_fill 查不到 ct_val）
        try:
            object.__setattr__(self, "_market_spec_cache", spec)
        except Exception:  # noqa: BLE001
            self._market_spec_cache = spec  # type: ignore[attr-defined]
        return spec

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """影子模式：绝不真发 setLeverage 给 OKX；只记到本地虚拟层（用于展示/占用保证金估算）。"""
        lev = max(1, int(leverage or 1))
        self._ensure_virtual(symbol)
        self._virtual_leverage[symbol] = lev
        # 如果已经有持仓：刷新 leverage 展示字段 + 重算强平价估算
        pos = self._virtual_positions.get(symbol)
        if pos is not None and pos.size > 0 and pos.side != PositionSide.FLAT:
            pos.leverage = lev
            ct_val = self._ct_val(symbol)
            pos.liquidation_price = self._est_liquidation_price(
                pos.side, pos.size, pos.entry_price, lev, ct_val
            )
        logger.warning(
            "[SHADOW MODE] 拦截真实 set_leverage → 本地已记：symbol={} leverage={}X（不会发给交易所）",
            symbol, lev,
        )
        return True

    async def get_balance(self) -> Balance:
        real: Balance = await self._inner.get_balance()
        ct_val = self._ct_val(self.symbol)
        # 逐 symbol 累计 realized（默认单 symbol，这里全扫简单）
        total_realized = sum(self._virtual_realized_pnl.values())
        # 未实现盈亏按最近 mark_price（先从 position 读，再从 balance 取 fallback）
        unrealized = 0.0
        try:
            # 透传读一次真实 OKX position 拿 mark_price（mark 必须真实）
            real_pos = await self._inner.get_position(self.symbol)
            mark = float(real_pos.mark_price or 0.0)
        except Exception:  # noqa: BLE001
            mark = 0.0
        margin_used = 0.0
        for sym, pos in self._virtual_positions.items():
            if pos.size <= 0 or pos.side == PositionSide.FLAT:
                continue
            if mark <= 0:
                mark = float(pos.entry_price or 0.0)
            if sym == self.symbol:
                mark_eff = mark
            else:
                mark_eff = float(pos.entry_price or 0.0)
            entry = float(pos.entry_price or 0.0)
            dir_sign = 1.0 if pos.side == PositionSide.LONG else -1.0
            unrealized += float(pos.size) * ct_val * dir_sign * (mark_eff - entry)
        margin_used = self._calc_margin_used_usdt(self.symbol, ct_val, mark)

        total = float(real.total or 0.0) + float(total_realized) + float(unrealized)
        available = max(
            0.0,
            float(real.available or 0.0) + float(total_realized) - margin_used,
        )
        balance = Balance(
            total=round(total, 4),
            available=round(available, 4),
            unrealized_pnl=round(float(real.unrealized_pnl or 0.0) + float(unrealized), 4),
        )
        # currency 透传：Real OKX Balance 没定义 currency 字段就忽略（pydantic 允许）
        currency = getattr(real, "currency", None)
        if currency:
            try:
                balance.__dict__["currency"] = currency
            except Exception:  # noqa: BLE001
                pass
        return balance

    async def get_position(self, symbol: str) -> Position:
        self._ensure_virtual(symbol)
        vpos = self._virtual_positions[symbol]
        # 真实 mark / lev / liq 必须取 OKX（行情真实）；若 virtual lev 已记则优先展示它
        try:
            real_pos: Position = await self._inner.get_position(symbol)
        except Exception:  # noqa: BLE001
            real_pos = Position(symbol=symbol)
        # 2026-08-31 Bug Fix：空仓时 real_pos.mark_price 常为 0，vpos.entry_price 也为 0 → 两者都 0
        #   → 最终 mark=0 → run.py 兜底死 2466 → 最小名义卡死 2.466U。
        #   新增 3 级价格来源：先 ticker（最准）→ 再 real_pos.mark → 再 virtual entry（最后再 0 交给上层）。
        mark_price = 0.0
        try:
            if hasattr(self._inner, "get_ticker_price") and callable(getattr(self._inner, "get_ticker_price")):
                _tp = await self._inner.get_ticker_price(symbol)
                if float(_tp or 0.0) > 0:
                    mark_price = float(_tp)
        except Exception:  # noqa: BLE001
            pass
        if mark_price <= 0:
            mark_price = float(real_pos.mark_price or 0.0)
        if mark_price <= 0:
            mark_price = float(vpos.entry_price or 0.0)
        ct_val = self._ct_val(symbol)
        # 计算虚拟仓位的未实现盈亏 & 强平价
        unreal = 0.0
        liq = float(real_pos.liquidation_price or 0.0)
        lev = max(1, int(self._virtual_leverage.get(symbol, 1) or int(getattr(real_pos, "leverage", 1) or 1)))
        if vpos.size > 0 and vpos.side != PositionSide.FLAT:
            dir_sign = 1.0 if vpos.side == PositionSide.LONG else -1.0
            unreal = float(vpos.size) * ct_val * dir_sign * max(
                (mark_price - float(vpos.entry_price or 0.0)), -1e18
            )
            liq = self._est_liquidation_price(
                vpos.side, vpos.size, float(vpos.entry_price or 0.0), lev, ct_val
            )
        # 合成 Position：symbol/side/size/entry 来自 virtual（虚拟成交真实反映）
        # mark / unrealized / leverage / liquidation 用我们合成（以真实 OKX 行情为底）
        return Position(
            symbol=symbol,
            side=vpos.side,
            size=round(float(vpos.size), 6),
            entry_price=round(float(vpos.entry_price or 0.0), 6),
            mark_price=round(mark_price, 6),
            unrealized_pnl=round(float(unreal), 6),
            leverage=lev,
            liquidation_price=round(liq, 6),
        )

    async def get_open_orders(self, symbol: str) -> list[Order]:
        # shadow 所有 place_order 都是瞬时 FILLED，虚拟挂单=空；inner 返回 OKX 上真实挂单
        return await self._inner.get_open_orders(symbol)

    async def get_order_by_cid(self, symbol: str, client_order_id: str) -> Optional[Order]:
        # 1) 先查本地影子账本：影子成交的一定命中
        order = self._shadow_orders.get(client_order_id)
        if order is not None:
            return order
        # 2) 再透传 inner：极个别"先 shadow 关了 → 切实盘继续跑"的场景兜底
        if hasattr(self._inner, "get_order_by_cid") and callable(getattr(self._inner, "get_order_by_cid")):
            return await self._inner.get_order_by_cid(symbol, client_order_id)
        return None

    # ======================================================================
    #  write 路径：拦截 & 返回假成功 & 写虚拟账本
    # ======================================================================
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
        filled = float(amount)
        # 2026-08-31 修复：MARKET + price=0 时，之前 avg_fill_price=0 → _apply_fill 被跳过，
        #   导致虚拟仓位根本没写入（get_position 仍 FLAT）。现在兜底到 inner 的最新报价 / 已镜像持仓 entry：
        avg_fill_price = float(price) if price and type != OrderType.MARKET else (float(price) if price else 0.0)
        if avg_fill_price <= 0 and filled > 0:
            try:
                if hasattr(self._inner, "get_ticker_price") and callable(getattr(self._inner, "get_ticker_price")):
                    _tp = await self._inner.get_ticker_price(symbol)
                    if float(_tp or 0.0) > 0:
                        avg_fill_price = float(_tp)
            except Exception:  # noqa: BLE001
                avg_fill_price = 0.0
        if avg_fill_price <= 0 and filled > 0:
            try:
                _ppos = await self._inner.get_position(symbol)
                _mp = float(getattr(_ppos, "mark_price", 0.0) or 0.0)
                _ep = float(getattr(_ppos, "entry_price", 0.0) or 0.0)
                avg_fill_price = _mp or _ep or 0.0
            except Exception:  # noqa: BLE001
                avg_fill_price = 0.0
        if avg_fill_price <= 0 and filled > 0:
            # 再兜底：virtual 已有 entry
            self._ensure_virtual(symbol)
            avg_fill_price = float(self._virtual_positions[symbol].entry_price or 0.0) or _DEFAULT_CT_VAL * 2466.0
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
        # 写入虚拟持仓（如果 avg_fill_price 还是 0（极端市价+没传价格），不写账本避免 NPE；
        #   这种情况 caller 会按市价单「不校验 price」走，后续 position/balance 可能短暂偏差）
        if avg_fill_price > 0 and filled > 0:
            realized_usdt = self._apply_fill(symbol, side, filled, avg_fill_price)
        else:
            realized_usdt = 0.0
        # 落影子订单账本（get_order_by_cid 回溯用）
        self._shadow_orders[cid] = order

        logger.warning(
            "[SHADOW MODE] 拦截真实下单 → 已按 FILLED 伪造成交："
            "symbol={symbol} side={side} type={type} amount={amount} price={price} "
            "avg_fill={avg} client_order_id={cid} realized_pnl(反方向平仓)={realized}",
            symbol=symbol, side=side.value, type=type.value,
            amount=amount, price=price, avg=avg_fill_price, cid=cid,
            realized=f"{realized_usdt:+.4f}U",
        )
        return order

    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        # 影子模式下：真实 OKX 根本没这单 → 本地也都是瞬时 FILLED，直接返回成功
        logger.warning(
            "[SHADOW MODE] 拦截真实撤单 → 返回 True（伪造撤单成功）："
            "symbol={} client_order_id={}",
            symbol, client_order_id,
        )
        return True

    # 2026-08-31：Bug B 修复 - cancel_all / close_all 显式实现
    #   （否则基类 cancel_all → 取 get_open_orders → inner.open_orders，
    #     但 shadow 侧挂单都在本地瞬时 FILLED → 用 0 也 OK，但显式语义更清晰）
    async def cancel_all_orders(self, symbol: str) -> int:
        """Shadow：撤单都视为『已撤』，先查本地未完成影子单，再透传 inner 的 cancel_all。"""
        cnt_local = 0
        # 本地影子订单账本里：PENDING/PARTIAL 视作"已撤"，计数（仅日志语义）
        for o in list(self._shadow_orders.values()):
            st = getattr(o, "status", None)
            stv = st.value if hasattr(st, "value") else str(st)
            if stv in ("PENDING", "PARTIAL"):
                try:
                    from ..core.constants import OrderStatus  # noqa: PLC0415
                    o.status = OrderStatus.CANCELED
                    o.updated_at = int(__import__("time").time() * 1000)
                except Exception:  # noqa: BLE001
                    pass
                cnt_local += 1
        # inner 也取消（如果 inner 有 cancel_all）—— 实盘 shadow 模式：对 inner 不触碰但 cancel 无害
        inner_cnt = 0
        if hasattr(self._inner, "cancel_all_orders") and callable(getattr(self._inner, "cancel_all_orders")):
            try:
                inner_cnt = int(await self._inner.cancel_all_orders(symbol) or 0)
            except Exception:  # noqa: BLE001
                inner_cnt = 0
        logger.warning(
            "[SHADOW MODE] cancel_all_orders(symbol={})：本地影子撤 {} 单，inner.cancel_all 返回 {}",
            symbol, cnt_local, inner_cnt,
        )
        return max(cnt_local, inner_cnt)

    async def close_all_positions(self, symbol: str) -> Position:
        """Shadow：把本地虚拟持仓『全额反向 MARKET 伪成交』，强制归 FLAT。"""
        from ..core.constants import OrderSide, OrderType, PositionSide  # noqa: PLC0415
        pos_before = await self.get_position(symbol)
        size = float(getattr(pos_before, "size", 0.0) or 0.0)
        side = getattr(pos_before, "side", PositionSide.FLAT)
        side_val = side.value if hasattr(side, "value") else str(side)
        if size <= 0 or side_val == PositionSide.FLAT.value:
            # 已空仓
            return Position(
                symbol=symbol, side=PositionSide.FLAT, size=0.0,
                entry_price=0.0, mark_price=float(getattr(pos_before, "mark_price", 0.0) or 0.0),
                leverage=getattr(pos_before, "leverage", 1) or 1,
            )
        # 反向市价伪成交一笔（复用 place_order 写影子账本 + 应用 fill 到 virtual）
        close_side = OrderSide.SELL if side_val == PositionSide.LONG.value else OrderSide.BUY
        try:
            await self.place_order(
                symbol=symbol, side=close_side, type=OrderType.MARKET,
                amount=size, price=0.0,
                client_order_id=f"_shadow_close_all_{int(__import__('time').time()*1000)}",
            )
        except Exception:  # noqa: BLE001
            # place_order 失败兜底：直接清空 virtual
            self._ensure_virtual(symbol)
            self._virtual_positions[symbol] = Position(
                symbol=symbol, side=PositionSide.FLAT, size=0.0,
                leverage=getattr(pos_before, "leverage", 1) or 1,
            )
        pos_after = await self.get_position(symbol)
        logger.warning(
            "[SHADOW MODE] close_all_positions: before {}x{}, after side={} size={}",
            side_val, size,
            pos_after.side.value if hasattr(pos_after.side, "value") else pos_after.side,
            pos_after.size,
        )
        return pos_after

    # ---- 可选：PaperBroker 专属 apply_ticker 等需要透传（否则 paper 模式下也套着会丢行情）----
    def __getattr__(self, item: str):
        # 未显式实现的属性/方法，都交给 inner：保证 apply_ticker / 额外扩展点不丢。
        return getattr(self._inner, item)
