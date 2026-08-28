# -*- coding: utf-8 -*-
"""交易总控（Controller）—— 聚合 Broker+Risk+AI+Storage，为 Dashboard 中文 API 提供数据。

所有 API 输出都使用**中文键名**（设计文档 · 第二十节）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from ..ai.base import AIProvider, MarketAnalysisResult, MarketData
from ..broker.base import Balance, Broker, Position
from ..core.config import AppConfig
from ..core.constants import (
    MarketRegime,
    OrderStatus,
    PositionSide,
    RunMode,
    SystemStatus,
)
from ..exchange.market import MarketDataProducer
from ..risk.engine import RiskEngine
from ..storage.state_store import StateStore
from ..storage.trade_journal import TradeJournal
from ..storage import journal_ext  # noqa: F401 —— 绑定 TradeJournal.append_market 便捷方法


_ZH_RUN_MODE = {RunMode.PAPER: "纸盘模式", RunMode.LIVE: "实盘模式"}
_ZH_SYSTEM_STATUS = {
    SystemStatus.RUNNING: "运行中",
    SystemStatus.STOPPED: "停止",
    SystemStatus.RECOVERING: "恢复中",
    SystemStatus.ERROR: "异常",
}
_ZH_POSITION = {PositionSide.LONG: "做多", PositionSide.SHORT: "做空", PositionSide.FLAT: "空仓"}
_ZH_ORDER_STATUS = {
    OrderStatus.PENDING: "待成交", OrderStatus.PARTIAL: "部分成交",
    OrderStatus.FILLED: "已成交", OrderStatus.CANCELED: "已撤销",
    OrderStatus.ERROR: "异常",
}
_ZH_MARKET = {
    MarketRegime.TREND_UP: "上涨趋势", MarketRegime.TREND_DOWN: "下跌趋势",
    MarketRegime.RANGE: "震荡", MarketRegime.HIGH_VOLATILITY: "高波动",
    MarketRegime.LOW_VOLATILITY: "低波动",
}


class TradingController:
    """交易总控：持有 Broker / AI / Risk / Storage，对外给 Dashboard 数据。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        broker: Broker,
        ai: AIProvider,
        risk: RiskEngine,
        state_store: StateStore,
        journal: TradeJournal,
        market_producer: Optional[MarketDataProducer] = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.ai = ai
        self.risk = risk
        self.state_store = state_store
        self.journal = journal
        self.market_producer = market_producer
        # 记录最近一次 AI 判断结果，供 Dashboard 展示
        self._last_ai: Optional[MarketAnalysisResult] = None
        self._last_ai_ts: Optional[int] = None
        # 从 state_store 恢复 last_ai
        self._restore_last_ai_from_store()

    # ------------------------------------------------------------------
    # 初始化：从 state_store 恢复 last_ai；若仍为空，用构造的 MarketData 调一次 AI 给初始值
    # ------------------------------------------------------------------
    def _restore_last_ai_from_store(self) -> None:
        import asyncio as _aio
        st = self.state_store.load()
        saved = st.get("last_ai") or {}
        if saved and saved.get("market_regime"):
            try:
                self._last_ai = MarketAnalysisResult(
                    market_regime=MarketRegime(saved["market_regime"]),
                    confidence=int(saved.get("confidence") or 0),
                    reason=str(saved.get("reason") or ""),
                )
                self._last_ai_ts = saved.get("ts")
            except Exception:
                self._last_ai = None
        # 若还没有（冷启动），用默认 MarketData 调一次 AI，让 Dashboard 首次展示不空
        if self._last_ai is None:
            try:
                md = MarketData(
                    symbol=self.config.trading.symbol,
                    timestamp=int(time.time() * 1000),
                )
                res = _aio.run(self.ai.analyze_market(md))
                self._last_ai = res
                self._last_ai_ts = int(time.time() * 1000)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 统一中文翻译辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _zh(d: dict, key, default: str = "—") -> str:
        return d.get(key, default) or default

    # ------------------------------------------------------------------
    # 数据源：中文
    # ------------------------------------------------------------------
    def get_status_dict(self) -> dict[str, Any]:
        """/api/status 中文响应。"""
        st = self.state_store.load()
        status_raw = st.get("status") or SystemStatus.STOPPED.value
        try:
            sys_status = SystemStatus(status_raw)
        except ValueError:
            sys_status = SystemStatus.STOPPED
        mode = self.config.trading.mode
        ai_block = self._last_ai_block()
        stats = st.get("stats") or {}

        return {
            "运行模式": _ZH_RUN_MODE.get(mode, str(mode)),
            "系统状态": _ZH_SYSTEM_STATUS.get(sys_status, str(status_raw)),
            "启动时间": st.get("started_at") or None,
            "账户余额总权益": st.get("balance", {}).get("total", 0.0),
            "可用保证金": st.get("balance", {}).get("available", 0.0),
            "未实现盈亏": st.get("balance", {}).get("unrealized_pnl", 0.0),
            "累计交易次数": stats.get("trades_total", 0),
            "盈利次数": stats.get("wins", 0),
            "亏损次数": stats.get("losses", 0),
            "累计收益率(%)": round(float(stats.get("total_pnl_pct") or 0), 2),
            "最近AI判断": ai_block,
            "风控状态": {
                "连续亏损次数": self.risk.consecutive_losses,
                "熔断冷却至(秒时间戳)": self.risk.cooldown_until_ts,
                "是否允许开仓": "是" if (self.risk.cooldown_until_ts == 0 or time.time() >= self.risk.cooldown_until_ts) and sys_status == SystemStatus.RUNNING else "否",
            },
        }

    def _last_ai_block(self) -> dict[str, Any]:
        if self._last_ai is None:
            return {"市场状态": "暂无", "置信度": 0, "理由": "系统尚未发起AI分析", "时间": None}
        return {
            "市场状态": _ZH_MARKET.get(self._last_ai.market_regime, str(self._last_ai.market_regime)),
            "置信度": self._last_ai.confidence,
            "理由": self._last_ai.reason,
            "时间": self._last_ai_ts,
        }

    # ------------------------------------------------------------------
    async def get_balance_dict(self) -> dict[str, Any]:
        """查询 Broker 实时余额（中文）。"""
        bal: Balance = await self.broker.get_balance()
        # 同步写回 state
        st = self.state_store.load()
        st["balance"] = {
            "total": bal.total,
            "available": bal.available,
            "unrealized_pnl": bal.unrealized_pnl,
        }
        self.state_store.save(st)
        return {
            "账户总权益": round(bal.total, 4),
            "可用保证金": round(bal.available, 4),
            "未实现盈亏": round(bal.unrealized_pnl, 4),
            "货币": "USDT",
        }

    async def get_position_dict(self) -> dict[str, Any]:
        """查询 Broker 实时持仓（中文）。"""
        pos: Position = await self.broker.get_position(self.config.trading.symbol)
        # 同步写回 state
        st = self.state_store.load()
        st["position"] = pos.model_dump(mode="json") if pos.side != PositionSide.FLAT else None
        self.state_store.save(st)

        pnl_pct = 0.0
        if pos.entry_price > 0 and pos.side != PositionSide.FLAT:
            delta = (pos.mark_price - pos.entry_price) if pos.side == PositionSide.LONG else (pos.entry_price - pos.mark_price)
            pnl_pct = (delta / pos.entry_price) * 100 * max(1, pos.leverage)

        return {
            "交易对": pos.symbol,
            "持仓方向": _ZH_POSITION.get(pos.side, str(pos.side)),
            "持仓数量": pos.size,
            "开仓均价": round(pos.entry_price, 6),
            "标记价格": round(pos.mark_price, 6),
            "未实现盈亏": round(pos.unrealized_pnl, 4),
            "未实现收益率(%)": round(pnl_pct, 3),
            "杠杆": pos.leverage,
            "强平价格": round(pos.liquidation_price, 6) if pos.liquidation_price else None,
        }

    # ------------------------------------------------------------------
    def get_recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近交易流水（倒序）。"""
        records = list(reversed(self.journal.read_all()))
        out: list[dict[str, Any]] = []
        for r in records[:limit]:
            regime = r.market_regime
            regime_str = regime.value if isinstance(regime, MarketRegime) else str(regime)
            zh_regime = _ZH_MARKET.get(MarketRegime(regime_str), regime_str) if regime_str in {e.value for e in MarketRegime} else regime_str
            out.append({
                "时间": r.time,
                "市场状态": zh_regime,
                "置信度": r.confidence,
                "入场原因": r.entry_reason,
                "结果": r.result,
                "附加信息": r.extra,
            })
        return out

    # ------------------------------------------------------------------
    async def analyze(self) -> dict[str, Any]:
        """拉行情 → 调 AI → 写入 last_ai，返回中文展示。

        若未配置 market_producer，则构造一份默认 MarketData 调 AI，保证注入的 AIProvider 始终被使用。
        """
        if self.market_producer is not None:
            md: MarketData = await self.market_producer.get_market_data()
        else:
            # 无行情生产者（单测/轻量启动），构造默认 MarketData，让 AI 仍可执行
            md = MarketData(
                symbol=self.config.trading.symbol,
                timestamp=int(time.time() * 1000),
            )
        result = await self.ai.analyze_market(md)
        self._last_ai = result
        self._last_ai_ts = int(time.time() * 1000)
        # 持久化到 state.json 便于恢复
        st = self.state_store.load()
        st["last_ai"] = {
            "market_regime": result.market_regime.value,
            "confidence": result.confidence,
            "reason": result.reason,
            "ts": self._last_ai_ts,
        }
        self.state_store.save(st)
        return {
            "市场状态": _ZH_MARKET.get(result.market_regime, str(result.market_regime)),
            "置信度": result.confidence,
            "理由": result.reason,
            "时间": self._last_ai_ts,
        }
