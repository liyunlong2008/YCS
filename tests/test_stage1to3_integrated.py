# -*- coding: utf-8 -*-
"""阶段 1-3 / 阶段 2-2 / 阶段 2-3 / 阶段 3-1 的集中测试文件：

 1. MarketDataProducer：OKX 拉 ticker + ohlcv → AI.MarketData
 2. TradingController：聚合 Broker+Risk+AI+Storage → 中文 Dashborad API 数据
 3. Recovery.recover：OKX 余额/持仓/挂单写回 StateStore，比较覆盖
 4. PaperBroker 撮合：按最新 ticker 成交 + 止盈/止损触发

全部 TDD：先写 failing 用例，再补实现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from app.ai.base import AIProvider, MarketAnalysisResult, MarketData
from app.api.app import create_app
from app.broker.base import Balance, Order, Position
from app.broker.paper import PaperBroker
from app.core.config import (
    AIConfig,
    AppConfig,
    OKXConfig,
    TradingConfig,
)
from app.core.constants import (
    MarketRegime,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RunMode,
    SystemStatus,
    SYMBOL,
    TIME_DRIFT_THRESHOLD,
)
from app.exchange.market import MarketDataProducer
from app.recovery.recoverer import SystemRecoverer
from app.risk.engine import RiskEngine
from app.services.controller import TradingController
from app.storage.state_store import StateStore
from app.storage.trade_journal import TradeJournal


# ======================================================================
# 通用 Fake OKX Exchange（fetch_ticker / fetch_ohlcv）
# ======================================================================
@dataclass
class FakeOKXExchange:
    """模拟 ccxt.pro.okx 行情接口。"""

    ticker: dict
    ohlcv_1h: list[list[float]]
    ohlcv_15m: list[list[float]]
    server_time_ms: int = 1_700_000_000_000
    balance: dict | None = None
    positions: list[dict] | None = None
    open_orders: list[dict] | None = None

    async def fetch_ticker(self, symbol: str, params: Any = None) -> dict:
        return self.ticker

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since: Optional[int] = None,
        limit: Optional[int] = None, params: Any = None,
    ) -> list[list[float]]:
        if timeframe == "1h":
            return list(self.ohlcv_1h)
        if timeframe == "15m":
            return list(self.ohlcv_15m)
        raise ValueError(f"未在测试中支持的 timeframe: {timeframe}")

    async def fetch_time(self) -> int:
        return self.server_time_ms

    async def fetch_balance(self, params=None) -> dict:
        return self.balance or {"USDT": {"total": 1000, "free": 900, "used": 100}, "info": {"data": [{}]}}

    async def fetch_positions(self, symbols=None, params=None) -> list[dict]:
        return self.positions or []

    async def fetch_open_orders(self, symbol=None, *a, **kw) -> list[dict]:
        return self.open_orders or []

    async def close(self) -> None:  # pragma: no cover
        return None


def _producer_with_fake(ex: FakeOKXExchange) -> MarketDataProducer:
    """构造一个注入 Fake exchange 的 MarketDataProducer。"""
    from app.exchange.market import MarketDataProducer

    prod = MarketDataProducer(okx=OKXConfig(api_key="K", secret="S", passphrase="P"), symbol=SYMBOL)
    prod._exchange = ex  # type: ignore[assignment]
    return prod


# ======================================================================
# 1. MarketDataProducer
# ======================================================================
def test_market_producer_builds_market_data_with_correct_fields() -> None:
    """MarketDataProducer 把 ticker + ohlcv 组合成符合 MarketData schema 的对象。"""
    t = {
        "timestamp": 1_700_000_000_000,
        "open": 2000.0, "high": 2050.0, "low": 1990.0,
        "close": 2040.0, "baseVolume": 3000.0, "quoteVolume": 3000.0 * 2020.0,
        "last": 2040.0, "bid": 2039.5, "ask": 2040.5,
    }
    h1 = [[i * 3_600_000, 2000 + i, 2010 + i, 1990 + i, 2005 + i, 100.0 + i] for i in range(48)]
    m15 = [[i * 15 * 60_000, 2000 + i, 2001 + i, 1999 + i, 2000 + i, 50 + i] for i in range(64)]

    fake = FakeOKXExchange(ticker=t, ohlcv_1h=h1, ohlcv_15m=m15)
    prod = _producer_with_fake(fake)
    md = asyncio.run(prod.get_market_data())

    assert md.symbol == SYMBOL
    assert md.timestamp == t["timestamp"]
    assert (md.open, md.high, md.low, md.close, md.volume) == (
        2000.0, 2050.0, 1990.0, 2040.0, 3000.0,
    )
    # K 线数量：最近 48 根 1H / 最近 64 根 15m
    assert len(md.ohlcv_1h) == 48
    assert len(md.ohlcv_15m) == 64
    # MarketData 可被 Pydantic 校验
    MarketData.model_validate(md.model_dump())


def test_market_producer_limits_candle_count() -> None:
    """即便交易所返回过量 K 线，最终 MarketData 上限为 max_candles 参数。"""
    h1 = [[i, 1, 2, 0, 1.5, 1.0] for i in range(200)]
    m15 = [[i, 1, 2, 0, 1.5, 1.0] for i in range(300)]
    t = {"timestamp": 1, "open": 1, "high": 2, "low": 0, "close": 1.5, "baseVolume": 10}
    fake = FakeOKXExchange(ticker=t, ohlcv_1h=h1, ohlcv_15m=m15)
    prod = _producer_with_fake(fake)
    md = asyncio.run(prod.get_market_data(max_candles=32))
    assert len(md.ohlcv_1h) == 32
    assert len(md.ohlcv_15m) == 32


# ======================================================================
# 2. TradingController + Dashboard 中文 API
# ======================================================================
class _StableAI(AIProvider):
    """返回稳定 AI 输出，便于断言中文展示。"""

    def __init__(self, regime: MarketRegime = MarketRegime.TREND_UP, confidence: int = 88, reason: str = "趋势完整") -> None:
        self.regime = regime
        self.confidence = confidence
        self.reason = reason

    async def analyze_market(self, market_data: MarketData) -> MarketAnalysisResult:
        return MarketAnalysisResult(
            market_regime=self.regime, confidence=self.confidence, reason=self.reason,
        )


def _make_controller(tmp_path: Path, *, mode: RunMode = RunMode.PAPER, ai: AIProvider | None = None) -> TradingController:
    cfg = AppConfig(
        okx=OKXConfig(api_key="", secret="", passphrase=""),
        ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat"),
        trading=TradingConfig(live=(mode == RunMode.LIVE), symbol=SYMBOL),
    )
    from app.broker.factory import build_broker
    broker = build_broker(cfg)
    store = StateStore(tmp_path)
    journal = TradeJournal(tmp_path)
    return TradingController(
        config=cfg,
        broker=broker,
        ai=ai or _StableAI(),
        risk=RiskEngine(),
        state_store=store,
        journal=journal,
    )


def test_controller_status_returns_chinese_status(tmp_path: Path) -> None:
    """系统状态 API：中文键（运行模式 / 系统状态 / AI 状态）。"""
    ctl = _make_controller(tmp_path, mode=RunMode.PAPER)
    state = ctl.get_status_dict()
    # 中文键
    assert state["运行模式"] == "纸盘模式"
    assert state["系统状态"] == "停止" or state["系统状态"] == "运行中"
    # 最近 AI 字段中文
    assert state["最近AI判断"]["市场状态"] in (
        "上涨趋势", "下跌趋势", "震荡", "高波动", "低波动"
    )


def test_controller_balance_and_position_chinese(tmp_path: Path) -> None:
    """余额 / 持仓：使用中文键。"""
    ctl = _make_controller(tmp_path)
    bal = asyncio.run(ctl.get_balance_dict())
    pos = asyncio.run(ctl.get_position_dict())
    assert "账户总权益" in bal
    assert "可用保证金" in bal
    assert "未实现盈亏" in bal
    assert "持仓方向" in pos
    assert pos.get("持仓方向") in ("空仓", "做多", "做空")


def test_controller_trade_list_and_ai_analyze(tmp_path: Path) -> None:
    """Trade Journal 返回最近 N 条；ai_analyze 能真正调用 AIProvider。"""
    ctl = _make_controller(tmp_path, ai=_StableAI(MarketRegime.RANGE, confidence=45, reason="震荡箱体"))
    ctl.journal.append_market(
        regime=MarketRegime.TREND_UP, confidence=88,
        entry_reason="突破", result="+1R",
    )
    ctl.journal.append_market(
        regime=MarketRegime.RANGE, confidence=40,
        entry_reason="区间低多", result="-0.5R",
    )
    # 最近 1 条
    last = ctl.get_recent_trades(limit=1)
    assert len(last) == 1
    assert last[0]["入场原因"] == "区间低多"

    # AI 分析中文
    analysis = asyncio.run(ctl.analyze())
    assert analysis["市场状态"] == "震荡"
    assert analysis["置信度"] == 45
    assert analysis["理由"] == "震荡箱体"


@pytest.mark.asyncio
async def test_dashboard_api_routes_return_chinese(tmp_path: Path) -> None:
    """FastAPI TestClient 走全链路，验证 JSON 字段全部中文。"""
    from fastapi.testclient import TestClient

    app = create_app()
    # 注入运行时 controller
    ctl = _make_controller(tmp_path)
    app.state.runtime["controller"] = ctl

    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert "运行模式" in body
        assert "系统状态" in body

        r = client.get("/api/balance")
        assert r.status_code == 200
        assert "账户总权益" in r.json()

        r = client.get("/api/position")
        assert r.status_code == 200
        assert "持仓方向" in r.json()

        r = client.get("/api/trades?limit=10")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        r = client.get("/api/ai/analyze")
        assert r.status_code == 200
        ai = r.json()
        assert "市场状态" in ai and "置信度" in ai and "理由" in ai


# ======================================================================
# 3. Recovery.recover 真实写回 + 时间漂移
# ======================================================================
def test_recovery_writes_okx_state_to_store(tmp_path: Path) -> None:
    """recover() 应将 OKX 余额 / 持仓 / 挂单覆盖到 state.json，状态从 RECOVERING → RUNNING。"""
    balance = {
        "USDT": {"total": 2500.0, "free": 2300.0, "used": 200.0},
        "info": {"data": [{"eq": "2500", "availEq": "2300", "upl": "42"}]},
    }
    positions = [{
        "symbol": SYMBOL, "side": "long", "contracts": 2.0,
        "entryPrice": 2000, "markPrice": 2050, "leverage": 3,
        "unrealizedPnl": 100, "liquidationPrice": 1900,
    }]
    open_orders = [{
        "id": "1", "clientOrderId": "YL-20260828-00888", "symbol": SYMBOL,
        "side": "buy", "type": "limit", "price": 2000, "amount": 1,
        "filled": 0, "average": 0, "status": "open",
        "timestamp": 1, "lastUpdateTimestamp": 2,
    }]
    fake = FakeOKXExchange(
        ticker={}, ohlcv_1h=[], ohlcv_15m=[],
        balance=balance, positions=positions, open_orders=open_orders,
        server_time_ms=int(__import__("time").time() * 1000),  # 漂移很小
    )
    from app.broker.okx_broker import OKXBroker as OKX
    brk = OKX(symbol=SYMBOL, okx=OKXConfig(api_key="K", secret="S", passphrase="P"))
    brk._exchange = fake  # type: ignore[assignment]
    store = StateStore(tmp_path)
    rec = SystemRecoverer(broker=brk, state_store=store)
    rec._last_sync_ok = True  # 跳过真实网络错误

    state = asyncio.run(rec.recover())

    assert state["status"] == SystemStatus.RUNNING.value
    # 余额回写
    assert state["balance"]["total"] == 2500.0
    assert state["balance"]["available"] == 2300.0
    assert state["balance"]["unrealized_pnl"] == 42.0
    # 持仓回写
    assert state["position"] is not None
    assert state["position"]["side"] == PositionSide.LONG.value
    assert state["position"]["size"] == 2.0
    assert state["position"]["entry_price"] == 2000.0
    # 挂单回写
    assert len(state["open_orders"]) == 1
    assert state["open_orders"][0]["client_order_id"] == "YL-20260828-00888"


def test_recovery_sync_time_drift_over_threshold_pauses(tmp_path: Path) -> None:
    """时间同步漂移 > 阈值（10s） → 状态置 STOPPED / 暂停开仓。"""
    fake = FakeOKXExchange(ticker={}, ohlcv_1h=[], ohlcv_15m=[])
    # 服务器时间 = 本地 - 20s → drift = +20s（本地比 OKX 快 20s）
    fake.server_time_ms = int(__import__("time").time() * 1000) - (TIME_DRIFT_THRESHOLD + 10) * 1000
    from app.broker.okx_broker import OKXBroker as OKX
    brk = OKX(symbol=SYMBOL, okx=OKXConfig(api_key="K", secret="S", passphrase="P"))
    brk._exchange = fake  # type: ignore[assignment]
    store = StateStore(tmp_path)
    # 预写入 RUNNING
    st = store.load()
    st["status"] = SystemStatus.RUNNING.value
    store.save(st)

    rec = SystemRecoverer(broker=brk, state_store=store)
    server_ms, drift_ms = asyncio.run(rec.sync_time())
    after = store.load()
    assert abs(drift_ms) >= (TIME_DRIFT_THRESHOLD + 10) * 1000 - 1  # 漂移确实超阈值
    assert after["status"] == SystemStatus.STOPPED.value


# ======================================================================
# 4. PaperBroker 真实撮合（按 ticker）+ 移动止损触发
# ======================================================================
def test_paper_broker_tick_fills_limit_long(tmp_path: Path) -> None:
    """PaperBroker：BUY LIMIT @ 2000，推送 ticker.last=1999 被 Fill；成交后 Position 正确更新。"""
    brk = PaperBroker(symbol=SYMBOL, initial_balance=1000.0)
    # 1. 挂买单 2000
    order = asyncio.run(brk.place_order(
        symbol=SYMBOL, side=OrderSide.BUY, type=OrderType.LIMIT,
        amount=1, price=2000.0, client_order_id="YL-20260828-001",
    ))
    assert order.status == OrderStatus.PENDING
    # 2. 推送一个高于买价的 tick，成交
    brk.apply_ticker(last=1999.0, mark=1999.0, bid=1998.5, ask=1999.5)
    # 3. 订单状态 & 持仓
    final = asyncio.run(brk.get_order_by_cid(SYMBOL, "YL-20260828-001"))
    assert final is not None
    assert final.status == OrderStatus.FILLED
    assert final.avg_fill_price == 1999.0
    pos = asyncio.run(brk.get_position(SYMBOL))
    assert pos.side == PositionSide.LONG
    assert pos.size == 1.0
    assert pos.entry_price == 1999.0


def test_paper_broker_stop_loss_market(tmp_path: Path) -> None:
    """PaperBroker：持仓 LONG，入场 2000。apply_ticker 跌破止损价 1800 → 自动平掉（后续可由 Controller 调用平仓，但先确保 Broker 端有 STOP 市价语义）。

    这里用 place_order STOP + apply_ticker 验证 STOP 单触发。
    """
    brk = PaperBroker(symbol=SYMBOL, initial_balance=1000.0)
    # 先开多 @2000
    brk.apply_ticker(last=2000.0, mark=2000.0, bid=1999.5, ask=2000.5)
    long = asyncio.run(brk.place_order(SYMBOL, OrderSide.BUY, OrderType.MARKET, amount=1, price=0,
                                       client_order_id="YL-20260828-002"))
    # MARKET 单应立即成交
    assert asyncio.run(brk.get_order_by_cid(SYMBOL, "YL-20260828-002")).status == OrderStatus.FILLED
    pos = asyncio.run(brk.get_position(SYMBOL))
    assert pos.side == PositionSide.LONG and pos.size == 1.0

    # 挂 STOP @ 1800（SELL）
    stop = asyncio.run(brk.place_order(SYMBOL, OrderSide.SELL, OrderType.STOP, amount=1, price=1800,
                                       client_order_id="YL-20260828-003"))
    assert stop.status == OrderStatus.PENDING

    # 价格快速跌破 1800
    brk.apply_ticker(last=1780.0, mark=1780.0, bid=1779.5, ask=1780.5)
    st = asyncio.run(brk.get_order_by_cid(SYMBOL, "YL-20260828-003"))
    assert st.status == OrderStatus.FILLED
    # 持仓已平
    assert asyncio.run(brk.get_position(SYMBOL)).side == PositionSide.FLAT


# ======================================================================
# fixtures 小工具：journal.append_market 是我们在 Controller 里新增的便利方法
# ======================================================================
@pytest.fixture(autouse=True)
def _ensure_journal_has_append_market():
    """给 TradeJournal 打一个便利方法 append_market，使上面的断言无需写太多样板。

    在 services.controller 中会把它真正定义出来。若还没实现，这里打补丁。
    """
    if not hasattr(TradeJournal, "append_market"):
        def _append_market(self, *, regime, confidence, entry_reason, result, **extra):
            from app.storage.trade_journal import TradeRecord
            self.append(TradeRecord(
                market_regime=regime, confidence=confidence,
                entry_reason=entry_reason, result=result, extra=extra,
            ))
        TradeJournal.append_market = _append_market
