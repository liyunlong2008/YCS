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
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RunMode,
    SystemStatus,
)
from ..exchange.market import MarketDataProducer
from ..risk.engine import RiskEngine, RiskVerdict
from ..storage.state_store import StateStore
from ..storage.trade_journal import TradeJournal
from ..storage import journal_ext  # noqa: F401 —— 绑定 TradeJournal.append_market 便捷方法
from ..trading.order_manager import OrderManager  # noqa: F401 —— 运行时装配、execute_trade_signal 中 getattr 使用


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
        st = self.state_store.load()
        saved = st.get("last_ai") if isinstance(st, dict) else None
        if isinstance(saved, dict):
            try:
                from ..ai.base import MarketAnalysisResult
                self._last_ai = MarketAnalysisResult(
                    market_regime=MarketRegime(saved.get("market_regime", MarketRegime.RANGE.value)),
                    confidence=int(saved.get("confidence") or 0),
                    reason=str(saved.get("reason") or ""),
                )
                self._last_ai_ts = saved.get("ts")
            except Exception:
                self._last_ai = None
        # 冷启动：用默认 RANGE 填充，避免 Dashboard 显示 "暂无" 且合法 MarketRegime
        if self._last_ai is None:
            from ..ai.base import MarketAnalysisResult
            self._last_ai = MarketAnalysisResult(
                market_regime=MarketRegime.RANGE,
                confidence=0,
                reason="冷启动：等待首次 AI 行情分析",
            )
            self._last_ai_ts = int(time.time() * 1000)

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
        # A7. 影子模式后缀：与 /api/diag runtime_mode 保持一致的中文心智
        mode_cn = _ZH_RUN_MODE.get(mode, str(mode))
        if bool(getattr(self.config.risk_limits, "shadow_mode", False)):
            mode_cn = f"{mode_cn}(影子 SHADOW)"
        ai_block = self._last_ai_block()
        stats = st.get("stats") or {}

        return {
            "运行模式": mode_cn,
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

        若未配置 market_producer 或拉取失败（超时/无网络），回退到默认 MarketData，
        确保 Dashboard 始终有可用结果展示。
        """
        from ..ai.base import MarketAnalysisResult
        if self.market_producer is not None:
            try:
                md: MarketData = await self.market_producer.get_market_data()
            except Exception as e:
                logger.warning("行情拉取失败（回退默认）：{}", e)
                md = MarketData(
                    symbol=self.config.trading.symbol,
                    timestamp=int(time.time() * 1000),
                )
        else:
            md = MarketData(
                symbol=self.config.trading.symbol,
                timestamp=int(time.time() * 1000),
            )
        try:
            result = await self.ai.analyze_market(md)
        except Exception as e:
            logger.warning("AI 分析失败（回退默认 RANGE）：{}", e)
            result = MarketAnalysisResult(
                market_regime=MarketRegime.RANGE,
                confidence=0,
                reason=f"AI 暂不可用: {e}",
            )
        self._last_ai = result
        self._last_ai_ts = int(time.time() * 1000)
        logger.bind(log_type="trade").info(
            "[AI 决策] 市场状态={} 置信度={} 理由={}",
            result.market_regime.value, result.confidence, result.reason,
        )
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

    # ------------------------------------------------------------------
    # 统计辅助
    # ------------------------------------------------------------------
    def _load_stats(self) -> dict:
        st = self.state_store.load()
        stats = st.get("stats")
        if not isinstance(stats, dict):
            stats = {
                "trades_opened": 0, "trades_closed": 0,
                "wins": 0, "losses": 0,
                "total_pnl_usdt": 0.0, "total_pnl_pct": 0.0,
            }
        return stats

    def _save_stats(self, stats: dict) -> None:
        st = self.state_store.load()
        st["stats"] = stats
        self.state_store.save(st)

    def update_stats_on_closed(self, realized_pnl_usdt: float) -> None:
        """一笔交易关闭时调用：累计 wins/losses、总盈亏 USDT、总收益率%。"""
        stats = self._load_stats()
        stats["trades_closed"] = int(stats.get("trades_closed", 0)) + 1
        if realized_pnl_usdt > 1e-9:
            stats["wins"] = int(stats.get("wins", 0)) + 1
        elif realized_pnl_usdt < -1e-9:
            stats["losses"] = int(stats.get("losses", 0)) + 1
        # 平保（0 附近）不计 wins/losses，但计入 closed
        stats["total_pnl_usdt"] = float(stats.get("total_pnl_usdt", 0.0)) + realized_pnl_usdt
        # 收益率% = 累计总盈亏 / daily_start_balance * 100
        base = max(float(self.risk.daily_start_balance or 0), 1e-9)
        stats["total_pnl_pct"] = (float(stats["total_pnl_usdt"]) / base) * 100
        self._save_stats(stats)
        logger.bind(log_type="trade").info(
            "[交易关闭] 已实现盈亏(USDT)={} 累计收益={}U({:.2f}%) 胜负={}-{}",
            round(realized_pnl_usdt, 6),
            round(float(stats["total_pnl_usdt"]), 6), float(stats["total_pnl_pct"]),
            stats.get("wins", 0), stats.get("losses", 0),
        )

    def _inc_opened_counter(self) -> None:
        stats = self._load_stats()
        stats["trades_opened"] = int(stats.get("trades_opened", 0)) + 1
        self._save_stats(stats)

    # ------------------------------------------------------------------
    # 交易执行：Maker 优先 → 超时 Taker 降级
    # ------------------------------------------------------------------
    async def execute_trade_signal(
        self,
        *,
        ai: MarketAnalysisResult,
        verdict: RiskVerdict,
        entry_price: float,
        market_side: OrderSide,
        maker_timeout: int = 20,
        poll_interval: float = 1.0,
        use_taker_fallback: bool = True,
        client_order_id: str | None = None,
    ) -> dict:
        """执行 AI 信号：风控拒绝则 REJECTED；否则 Maker 挂 LIMIT → 超时则 Taker 市价。

        Returns dict keys: status / via(=maker|taker|none) / order_id / avg_fill_price / qty / reason.
        """
        # 1) 风控拒绝
        if not verdict.allow:
            logger.bind(log_type="trade").warning(
                "[开仓被拒] 市场={} side={} 风控原因={}",
                ai.market_regime.value, market_side.value, verdict.reason,
            )
            return {"status": "REJECTED", "via": "none", "reason": verdict.reason,
                    "order_id": None, "avg_fill_price": 0.0, "qty": 0.0}

        symbol = self.config.trading.symbol
        amount = max(float(verdict.suggested_size or 0), 0)
        if amount <= 0:
            return {"status": "REJECTED", "via": "none", "reason": "建议数量<=0",
                    "order_id": None, "avg_fill_price": 0.0, "qty": 0.0}

        # 2) 设置建议杠杆（若 Broker 支持）
        if verdict.suggested_leverage and verdict.suggested_leverage > 0:
            setter = getattr(self.broker, "set_leverage", None)
            if callable(setter):
                try:
                    await setter(symbol, verdict.suggested_leverage)
                except Exception:
                    logger.warning("设置杠杆失败: {}", verdict.suggested_leverage)

        om: OrderManager | None = getattr(self, "order_manager", None)
        if om is None:
            # 降级：直接用 Broker 市价单（无 OrderManager 时）
            logger.warning("Controller 未挂载 OrderManager，降级 Broker 市价直连")
            order = await self.broker.place_order(
                symbol=symbol, side=market_side, type=OrderType.MARKET,
                amount=amount, client_order_id=client_order_id,
            )
            return {
                "status": order.status.value, "via": "taker",
                "order_id": order.client_order_id,
                "avg_fill_price": order.avg_fill_price, "qty": order.filled,
                "reason": "OrderManager 未挂载",
            }

        # 3) Maker 优先
        limit_price = float(entry_price)
        order = await om.submit_maker_then_cancel(
            symbol=symbol, side=market_side, amount=amount, price=limit_price,
            timeout=maker_timeout, poll_interval=poll_interval,
            client_order_id=client_order_id,
        )
        # 判定 Maker 是否成功成交（完全成交）
        if order is not None and order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL) and order.filled > 0:
            filled_ratio = order.filled / max(order.amount, 1e-12)
            remaining = max(order.amount - order.filled, 0)
            if remaining < 1e-9 or filled_ratio >= 1.0:
                logger.bind(log_type="trade").success(
                    "[Maker 成交] {} side={} qty={:.6f} @ price={} (entry_target={})",
                    order.client_order_id, market_side.value, order.filled,
                    order.avg_fill_price or order.price, entry_price,
                )
                self._inc_opened_counter()
                return {
                    "status": order.status.value, "via": "maker",
                    "order_id": order.client_order_id,
                    "avg_fill_price": float(order.avg_fill_price or order.price),
                    "qty": float(order.filled), "reason": "Maker 完全成交",
                }
            if use_taker_fallback and remaining > 0:
                await self.broker.place_order(
                    symbol=symbol, side=market_side, type=OrderType.MARKET,
                    amount=remaining,
                )

        # 4) Maker 未成交（超时或完全未成交）→ 降级 Taker 市价
        if not use_taker_fallback:
            return {
                "status": order.status.value if order else "CANCELED", "via": "maker",
                "order_id": order.client_order_id if order else None,
                "avg_fill_price": float(order.avg_fill_price) if order else 0.0,
                "qty": float(order.filled) if order else 0.0,
                "reason": "Maker 未成交且未启用 Taker 降级",
            }

        taker_order = await self.broker.place_order(
            symbol=symbol, side=market_side, type=OrderType.MARKET,
            amount=amount,
        )
        logger.bind(log_type="trade").warning(
            "[Taker 降级成交] side={} qty={:.6f} @ {} （Maker 超时/未成交）",
            market_side.value, taker_order.filled or amount,
            taker_order.avg_fill_price or "市价撮合",
        )
        if (taker_order.filled or 0) > 0 or taker_order.status == OrderStatus.FILLED:
            self._inc_opened_counter()
        return {
            "status": taker_order.status.value, "via": "taker",
            "order_id": taker_order.client_order_id,
            "avg_fill_price": float(taker_order.avg_fill_price or 0.0),
            "qty": float(taker_order.filled or amount),
            "reason": "Maker → Taker 降级成交",
        }

    # ------------------------------------------------------------------
    # 利润保护平仓
    # ------------------------------------------------------------------
    async def close_position_for_protection(self, position) -> float:
        """检测到 should_close_for_protection 后调用：市价反向平仓全仓。

        返回已实现盈亏（USDT），并通知 RiskEngine.on_trade_closed、更新 stats、
        重置 PositionManager、写入 journal。
        """
        from ..core.constants import OrderType as _OT
        if position.side == PositionSide.FLAT or position.size <= 0:
            return 0.0
        close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        before = (await self.broker.get_balance()).total
        try:
            await self.broker.place_order(
                symbol=position.symbol or self.config.trading.symbol,
                side=close_side,
                type=_OT.MARKET,
                amount=float(position.size),
            )
        except Exception:
            logger.exception("利润保护平仓下单失败")
            return 0.0
        after = (await self.broker.get_balance()).total
        realized = after - before
        # 通知风控
        self.risk.on_trade_closed(realized)
        # 统计
        self.update_stats_on_closed(realized)
        # 重置利润保护
        pm = getattr(self, "position_manager", None)
        if pm is not None:
            try:
                pm.reset()
            except Exception:
                pass
        # 流水日志
        try:
            result_str = f"{realized:+.3f}USDT"
            self.journal.append_market(
                regime=(self._last_ai.market_regime if self._last_ai else MarketRegime.RANGE),
                confidence=(self._last_ai.confidence if self._last_ai else 0),
                entry_reason="利润保护自动平仓",
                result=result_str,
                close_side=close_side.value,
                realized_usdt=round(realized, 6),
            )
        except Exception:
            logger.exception("写入 journal 失败（不影响主流程）")
        return realized

    # ------------------------------------------------------------------
    # 日切点
    # ------------------------------------------------------------------
    def apply_daily_reset_if_needed(self, today: str | None = None) -> bool:
        """检测日期变化 → 触发 risk.start_new_day()，返回 True 表示发生了切换。"""
        import datetime as _dt
        today = today or _dt.date.today().isoformat()
        st = self.state_store.load()
        last_day = st.get("daily_reset_day")
        if last_day == today:
            return False
        bal_total = float((st.get("balance") or {}).get("total", 0.0))
        if bal_total <= 0:
            try:
                import asyncio as _aio
                bal = _aio.get_event_loop().run_until_complete(self.broker.get_balance())
                bal_total = float(getattr(bal, "total", 0.0) or 0)
            except Exception:
                bal_total = 0.0
        if bal_total > 0:
            self.risk.start_new_day(bal_total)
        st["daily_reset_day"] = today
        self.state_store.save(st)
        logger.info("[日切点] 日期切换到 {}，risk.daily_start_balance={:.4f}U", today, bal_total)
        return True
