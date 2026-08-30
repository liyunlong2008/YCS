# -*- coding: utf-8 -*-
"""Broker 统一抽象（设计文档 · 第八节）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field

from ..core.constants import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SYMBOL,
)


# ----------------------------------------------------------------------
# MarketSpec：交易所交易规则快照（2026-08-30 新增，避免 RiskEngine 硬编码 minSz/张面值/杠杆上限）
#   · 实盘通过 OKXBroker.fetch_market_spec 真实从 /api/v5/public/instruments / load_markets 取；
#   · 纸盘/测试通过注入默认 ETH-USDT-SWAP 规则（与 OKX 线上保持一致）；
#   · 所有字段对 ETH-USDT-SWAP 的意义：
#       symbol=instId；ctVal=0.01(每张 0.01 ETH)；minSz=0.1(最小可下 0.1 张 ≈ 2.5 U)；
#       lotSz=0.1(张步进，只能 0.1 的整数倍)；szDecimals=1（amount 保留 1 位小数）；
#       tickSz=0.1(价格步进 0.1$)；minNotional_usdt=2.5 / 2.466（OKX 硬下限）；
#       maxLever=125（最大杠杆，实际可用杠杆 = min(config.default_leverage, maxLever)）；
#       max_limit_sz/max_market_sz = maxLmtSz / maxMktSz（从 OKX 返回，通常 >= 10000，不用担⼼）
# ----------------------------------------------------------------------
class MarketSpec(BaseModel):
    """交易所 symbol 的最小下单规则 / 面值 / 杠杆上限快照（USDT 口径）。"""
    symbol: str = SYMBOL
    # 张面值：1 张 = ctVal 单位的 base（ETH-USDT-SWAP=0.01 ETH → 1 张名义=0.01*mark_price USDT）
    ct_val: float = 0.01
    # 最小下单（张数，交易所单位 sz）
    min_sz: float = 0.1
    # 数量步进（张数必须为 lot_sz 的整数倍）
    lot_sz: float = 0.1
    # 数量小数位（OKX 对 LOT_SIZE 过滤的结果精度：min(Decimal(str(lot_sz)).as_tuple().exponent 位数, 4)）
    sz_decimals: int = 1
    # 价格步进（用于止损价 round，暂时用不强制）
    tick_sz: float = 0.1
    # 下单名义硬下限（USDT）；若交易所未返回 minNotional，取 min_sz * ct_val * entry 估算
    min_notional_usdt: float = 0.0
    # 杠杆上限
    max_lever: int = 125
    # 最大限价 / 市价张数（上限兜底，一般非常大）
    max_limit_sz: float = 10_000.0
    max_market_sz: float = 10_000.0
    # 来源：okx_instruments / ccxt_load_markets / fallback_defaults（方便日志/诊断区分）
    source: str = "fallback_defaults"

    # ------------------------------------------------------------------
    # 辅助：名义（USDT）↔ 张数 换算 + 合法性 round / clamp
    # ------------------------------------------------------------------
    def notional_to_sz(self, notional_usdt: float, entry_price: float) -> float:
        """目标名义（USDT）→ 在允许的张步进下的最大张数（向下对齐到 lot_sz）。"""
        if entry_price <= 0:
            return 0.0
        per_contract = self.ct_val * entry_price
        if per_contract <= 0:
            return 0.0
        raw = notional_usdt / per_contract
        return self.floor_sz(raw)

    def sz_to_notional(self, sz: float, entry_price: float) -> float:
        """张数 → 名义（USDT）= sz × ct_val × entry。"""
        return max(0.0, float(sz or 0.0)) * self.ct_val * max(entry_price, 1e-9)

    def floor_sz(self, raw_sz: float) -> float:
        """把 raw_sz 按 lot_sz 向下对齐，再按 sz_decimals 做 round（避免浮点尾差）。"""
        if raw_sz <= 0 or self.lot_sz <= 0:
            return 0.0
        quantized = int(raw_sz / self.lot_sz) * self.lot_sz
        # 避免类似 0.30000000000000004 的浮点尾差
        return round(float(quantized), self.sz_decimals)

    def effective_min_notional(self, entry_price: float, config_min: float = 0.0) -> float:
        """最终生效的最小名义 = max(交易所 minNotional, 按 minSz 折算名义, 配置层 config_min)。"""
        by_sz = self.min_sz * self.ct_val * max(entry_price, 0.0)
        return max(self.min_notional_usdt, by_sz, float(config_min or 0.0))

    def effective_max_sz(self, is_market: bool = False) -> float:
        return self.max_market_sz if is_market else self.max_limit_sz

    def clamp_sz(self, raw_sz: float, is_market: bool = False) -> float:
        """把 raw_sz 做 legality 规范化：floored to lot_sz、夹到 [min_sz, max_sz]、round to decimals。"""
        sz = self.floor_sz(raw_sz)
        upper = self.effective_max_sz(is_market)
        if sz > upper:
            sz = self.floor_sz(upper)
        if sz < self.min_sz:
            return 0.0
        return round(float(sz), self.sz_decimals)


class Balance(BaseModel):
    """账户余额（USDT）。"""
    total: float = 0.0        # 账户总权益
    available: float = 0.0    # 可用保证金
    unrealized_pnl: float = 0.0  # 未实现盈亏


class Position(BaseModel):
    """持仓信息（V1 单仓位）。"""
    symbol: str
    side: PositionSide = PositionSide.FLAT
    size: float = 0.0         # 合约张数
    entry_price: float = 0.0  # 开仓均价
    mark_price: float = 0.0   # 标记价格
    unrealized_pnl: float = 0.0
    leverage: int = 1
    liquidation_price: float = 0.0


class Order(BaseModel):
    """订单对象。"""
    client_order_id: str                          # YL-YYYYMMDD-XXXXX
    order_id: str = ""                            # 交易所返回 ID
    symbol: str
    side: OrderSide
    type: OrderType
    price: float = 0.0
    amount: float = 0.0                           # 下单数量
    filled: float = 0.0                           # 已成交数量
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: int = Field(default_factory=lambda: __import__("time").time_ns() // 1_000_000)
    updated_at: int = created_at


class Broker(ABC):
    """Broker 统一接口。

    子类：
      - PaperBroker：本地模拟成交
      - OKXBroker：调用 OKX 实盘接口
    """

    # ------------------------------------------------------------------
    # 行情 / 状态
    # ------------------------------------------------------------------
    @abstractmethod
    async def get_server_time_ms(self) -> int:
        """获取交易所服务器时间（毫秒）。"""

    @abstractmethod
    async def get_balance(self) -> Balance:
        """查询账户余额。"""

    @abstractmethod
    async def get_position(self, symbol: str) -> Position:
        """查询当前持仓（V1 单仓位）。"""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[Order]:
        """查询当前未成交订单。"""

    # 2026-08-30：新增；返回 symbol 的最小下单 / 面值 / 杠杆上限快照
    async def fetch_market_spec(self, symbol: Optional[str] = None) -> MarketSpec:
        """获取交易对的下单规则（minSz / ctVal / minNotional / maxLever 等）。

        子类可选重写：实盘 OKXBroker 用 load_markets + /api/v5/public/instruments 查真；
        默认实现返回 fallback 的 ETH-USDT-SWAP 默认值，供 PaperBroker / 测试兜底。"""
        return MarketSpec(symbol=(symbol or SYMBOL))

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: float,
        price: float = 0.0,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """提交订单。

        Args:
            symbol: 交易对（如 ETH-USDT-SWAP）
            side: BUY / SELL
            type: LIMIT / MARKET / STOP
            amount: 下单数量（合约张数）
            price: 限价单价格；市价单可为 0
            client_order_id: 客户端自定义订单号（幂等 / 恢复用）
        """

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        """撤销订单。成功返回 True。"""
