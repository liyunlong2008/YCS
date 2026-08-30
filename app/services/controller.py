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
from ..core.ai_throttle import AIThrottler, ThrottleDecision  # 新增 2026-08-30 自适应节流+价格哨兵


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
        # AI 节流器：2026-08-30 自适应 7 级状态机+价格哨兵
        self.ai_throttler: AIThrottler = AIThrottler()
        # 记录最近一次 AI 判断结果，供 Dashboard 展示
        self._last_ai: Optional[MarketAnalysisResult] = None
        self._last_ai_ts: Optional[int] = None
        # 从 state_store 恢复 last_ai + throttler
        self._restore_last_ai_from_store()
        # 恢复 throttler 持久化状态（注意：_restore_last_ai_from_store 已 load 过 state_store）
        st2 = self.state_store.load()
        self.ai_throttler.load_from(st2)
        # 若还没设过价格锚（冷启动），预填一个价格锚：否则前 30s~1m 哨兵没基准。
        if self.ai_throttler.state.sentinel_mark_price <= 0:
            # 冷启动先用 ETH≈2466 的保守参考价，后续 AI 真正调用时会刷新到真实 mark_price
            self.ai_throttler.state.sentinel_mark_price = 2466.0
            self.ai_throttler.state.sentinel_anchor_ts = int(time.time())
            st2b = self.state_store.load()
            self.ai_throttler.persist_to(st2b)
            self.state_store.save(st2b)

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
        import time as _t
        now_ts = int(_t.time())
        st = self.state_store.load()
        # 刷新 throttler 持久化态（冷启动或跨天后保证 to_status_dict 输出正确）
        self.ai_throttler.load_from(st)
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
        stats = self._load_stats()  # 2026-08-30: 用统一默认字典（含 trades_opened/closed/wins/losses 全字段默认值），避免 trades_total=None
        # 累计交易次数 = 已开 + 已平 / 2（单向单边统计一次）；更保守直接取「已开」的次数（=执行过 execute FILLED 的次数）
        stats.setdefault("trades_total", max(
            int(stats.get("trades_opened", 0)),
            int(stats.get("trades_closed", 0)),
        ))

        # AI 节流状态（2026-08-30 新增）：实时算一遍 sentinel/level，把最新 mark 价作为输入让波动%准
        mark_input = float((st.get("position") or {}).get("mark_price", 0.0) or 0.0)
        try:
            _ = self.ai_throttler.should_call_ai(
                now_ts=now_ts,
                system_status_running=(sys_status == SystemStatus.RUNNING),
                has_position=bool(st.get("position") and (st["position"].get("size") or 0) > 0),
                allow_trading=bool(
                    (self.risk.cooldown_until_ts == 0 or now_ts >= self.risk.cooldown_until_ts)
                    and sys_status == SystemStatus.RUNNING
                ),
                mark_price=mark_input if mark_input > 0 else 2466.0,
                entry_price=float((st.get("position") or {}).get("entry_price", 0.0) or 0.0),
                liquidation_price=float((st.get("position") or {}).get("liquidation_price", 0.0) or 0.0),
            )
        except Exception:  # noqa: BLE001
            pass
        throttle_block = self.ai_throttler.to_status_dict(now_ts)

        # 时间同步状态（Dashboard 顶部漂移 tag 数据来源）
        time_sync_raw = st.get("time_sync") or {}
        drift_ms = int(time_sync_raw.get("drift_ms") or 0)
        if abs(drift_ms) >= 1000:
            drift_txt = f"{drift_ms/1000:.2f}s"
        else:
            drift_txt = f"{drift_ms:.0f}ms"
        last_sync_at = int(time_sync_raw.get("last_sync_at") or 0)
        sync_age_s = now_ts - last_sync_at if last_sync_at > 0 else None
        if sync_age_s is not None and sync_age_s < 60:
            age_txt = f"{sync_age_s}s 前同步"
        elif sync_age_s is not None and sync_age_s < 3600:
            age_txt = f"{sync_age_s//60}m{sync_age_s%60:02d}s 前同步"
        elif sync_age_s is not None:
            h, m = divmod(sync_age_s, 3600); m, _ = divmod(m, 60)
            age_txt = f"{h}h{m:02d}m 前同步"
        else:
            age_txt = "未同步"
        drifted_pause = bool(time_sync_raw.get("drifted_pause"))
        sync_tag_cn = f"时间漂移 {drift_txt}{' ⚠️已暂停开仓' if drifted_pause else ''} · {age_txt}"
        sync_color = (
            "background:#fce8e6;color:#c5221f"
            if drifted_pause or abs(drift_ms) >= 5000
            else ("background:#feefc3;color:#8a6500" if abs(drift_ms) >= 1000
                  else "background:#e6f4ea;color:#137333")
        )
        time_sync_block = {
            "漂移毫秒": drift_ms,
            "漂移文本": drift_txt,
            "最后同步时间戳": last_sync_at or None,
            "同步距今年代": age_txt,
            "是否因漂移暂停": drifted_pause,
            "顶部标签文本": sync_tag_cn,
            "顶部标签颜色": sync_color,
            "阈值秒": 10,
        }

        # 启动时间：统一 started_at 为 int epoch（兼容字符串老数据）+ 人类可读文本 + 运行时长
        raw_sa = st.get("started_at")
        import datetime as _dt_sa  # noqa: PLC0415
        started_at_epoch: int | None = None
        started_at_local: str | None = None
        uptime_s: int | None = None
        uptime_human: str | None = None
        if isinstance(raw_sa, int) and raw_sa > 0:
            started_at_epoch = raw_sa
        elif isinstance(raw_sa, float) and raw_sa > 0:
            started_at_epoch = int(raw_sa)
        elif isinstance(raw_sa, str):
            try:
                started_at_epoch = int(_dt_sa.datetime.strptime(raw_sa, "%Y-%m-%d %H:%M:%S").timestamp())
            except Exception:  # noqa: BLE001
                started_at_epoch = None
        if started_at_epoch is not None:
            try:
                started_at_local = _dt_sa.datetime.fromtimestamp(started_at_epoch).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                started_at_local = None
            upt_s = now_ts - started_at_epoch
            if upt_s >= 0:
                uptime_s = upt_s
                h, rem = divmod(upt_s, 3600); m, s_ = divmod(rem, 60)
                uptime_human = (f"{h}h{m:02d}m{s_:02d}s" if h > 0
                                else f"{m}m{s_:02d}s" if m > 0 else f"{s_}s")
            else:
                # 时钟漂移 / 未来时区错觉：归零不显示负数
                uptime_s = 0
                uptime_human = "0s"

        return {
            "运行模式": mode_cn,
            "系统状态": _ZH_SYSTEM_STATUS.get(sys_status, str(status_raw)),
            "启动时间戳(epoch秒)": started_at_epoch,
            "启动时间": started_at_local,
            "运行时长(秒)": uptime_s,
            "运行时长": uptime_human,
            "账户余额总权益": st.get("balance", {}).get("total", 0.0),
            "可用保证金": st.get("balance", {}).get("available", 0.0),
            "未实现盈亏": st.get("balance", {}).get("unrealized_pnl", 0.0),
            "累计交易次数": stats.get("trades_total", 0),
            "盈利次数": stats.get("wins", 0),
            "亏损次数": stats.get("losses", 0),
            "累计收益率(%)": round(float(stats.get("total_pnl_pct") or 0), 2),
            "最近AI判断": ai_block,
            "AI节流状态": throttle_block,
            "时间同步状态": time_sync_block,
            "风控状态": {
                "连续亏损次数": self.risk.consecutive_losses,
                "熔断冷却至(秒时间戳)": self.risk.cooldown_until_ts,
                "是否允许开仓": "是" if (self.risk.cooldown_until_ts == 0 or time.time() >= self.risk.cooldown_until_ts) and sys_status == SystemStatus.RUNNING else "否",
                # 2026-08-30 新增：Dashboard 直读「最近一次风控结论/建议名义/缺口」，解决用户反馈的
                #   '风控显示允许但实盘影子从启动至今没开仓' 的可观测黑盒
                "最近一次风控": {
                    "时间戳": (
                        int(self.risk.last_verdict_at) if isinstance(getattr(self.risk, "last_verdict_at", 0), (int, float)) else 0
                    ),
                    "结论":
                        "通过" if (
                            self.risk.last_verdict is not None
                            and bool(getattr(self.risk.last_verdict, "allow", False))
                        ) else (
                            "拒绝" if self.risk.last_verdict is not None else "未执行"
                        ),
                    "原因": (
                        str(getattr(self.risk.last_verdict, "reason", ""))
                        if self.risk.last_verdict is not None
                        else "系统尚未发起风控评估（等下一轮主循环 10s 内）"
                    ),
                    "建议杠杆(X)":
                        int(getattr(self.risk.last_verdict, "suggested_leverage", 0) or 0)
                        if self.risk.last_verdict is not None else None,
                    "建议名义价值(USDT)": round(
                        float(getattr(self.risk.last_verdict, "suggested_notional_usdt", 0.0) or 0.0), 4
                    ) if self.risk.last_verdict is not None else None,
                    "最小名义(USDT)": round(
                        float(getattr(self.risk.last_verdict, "effective_min_notional_usdt", 0.0) or 0.0), 4
                    ) if self.risk.last_verdict is not None else None,
                    "缺口本金(USDT)": (
                        # 缺口 = 摸到最小单还需要补多少本金（= (min_notional - 当前目标名义)/leverage）
                        round(max(0.0, (
                            float(getattr(self.risk.last_verdict, "effective_min_notional_usdt", 0.0) or 0.0)
                            - float(getattr(self.risk.last_verdict, "suggested_notional_usdt", 0.0) or 0.0)
                        )) / max(1, int(getattr(self.risk.last_verdict, "suggested_leverage", 1) or 1)), 4)
                        if (self.risk.last_verdict is not None and not bool(getattr(self.risk.last_verdict, "allow", False)))
                        else None
                    ),
                    "AI_信号状态": (
                        "不足(reg=%s conf=%s，≥50且TREND才开)" % (
                            (ai_block.get("市场状态") or "?"),
                            (ai_block.get("置信度") if isinstance(ai_block, dict) else 0),
                        )
                        if (not isinstance(ai_block, dict)
                            or int(ai_block.get("置信度") or 0) < 50
                            or (ai_block.get("市场状态") or "") in ("低波动", "震荡区间", "暂无"))
                        else "到位: %s conf=%s" % (
                            (ai_block.get("市场状态") or "?"),
                            (ai_block.get("置信度") if isinstance(ai_block, dict) else 0),
                        )
                    ),
                },
                # 最近一次通过风控+AI双确认→进入下单流程的时间戳；=0 意味着从未准备下单
                "最近一次交易信号就绪时间戳": int(getattr(self.risk, "last_pass_trade_signal_at", 0) or 0),
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
    # AI 节流 + 价格哨兵（新增 2026-08-30：7 级状态机自适应调用频率）
    # ------------------------------------------------------------------
    async def should_analyze(
        self,
        *,
        mark_price: float = 0.0,
        entry_price: float = 0.0,
        stop_loss_price: float = 0.0,
        take_profit_price: float = 0.0,
        liquidation_price: float = 0.0,
        has_position: bool = False,
        force: bool = False,
    ) -> ThrottleDecision:
        """主循环入口：本轮是否需要调 AI analyze()。
        - 自动读 state_store：RUNNING 状态、风控 allow_trading、冷却 cooldown_until；
        - mark_price 不传时自动查 broker 仓位 mark_price → state_store → 兜底 2466；
        - 返回 ThrottleDecision：should_call / level / reason / early_wake（行情≥1% 波动叫醒）/ event_pct。
        """
        now_ts = int(time.time())
        st = self.state_store.load()
        self.ai_throttler.load_from(st)
        # 系统状态 & 风控 allow
        status_raw = st.get("status") or SystemStatus.STOPPED.value
        try:
            running = SystemStatus(status_raw) == SystemStatus.RUNNING
        except ValueError:
            running = False
        risk_dict = st.get("risk") or {}
        cd_ts = int(risk_dict.get("cooldown_until", 0) or 0)
        allow = bool(risk_dict.get("allow_trading", True)) and (cd_ts == 0 or now_ts >= cd_ts)
        # 价格 & 仓位实时获取
        mark = float(mark_price or 0.0)
        has_pos = bool(has_position)
        entry = float(entry_price or 0.0)
        liq = float(liquidation_price or 0.0)
        if mark <= 0:
            try:
                pos = await self.broker.get_position(self.config.trading.symbol)
                mark = float(getattr(pos, "mark_price", 0.0) or 0.0)
                from ..core.constants import PositionSide as _PS
                has_pos = (pos.side != _PS.FLAT)
                if entry <= 0:
                    entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if liq <= 0:
                    liq = float(getattr(pos, "liquidation_price", 0.0) or 0.0)
            except Exception:  # noqa: BLE001
                mark = 0.0
        if mark <= 0:
            pos_save = (st.get("position") or {}) if isinstance(st, dict) else {}
            mark = float(pos_save.get("mark_price", 0.0) or 0.0)
            if entry <= 0:
                entry = float(pos_save.get("entry_price", 0.0) or 0.0)
        if mark <= 0:
            mark = 2466.0  # 兜底

        dec = self.ai_throttler.should_call_ai(
            now_ts=now_ts,
            system_status_running=running,
            has_position=has_pos,
            allow_trading=allow,
            mark_price=mark,
            entry_price=entry,
            stop_loss_price=float(stop_loss_price or 0.0),
            take_profit_price=float(take_profit_price or 0.0),
            liquidation_price=liq,
            force=bool(force),
        )
        # 持久化：哪怕本轮不调 AI，价格哨兵的 last_event_pct / last_event_at / level 也要存
        st2 = self.state_store.load()
        self.ai_throttler.persist_to(st2)
        self.state_store.save(st2)
        return dec

    # ------------------------------------------------------------------
    async def analyze(self, force: bool = False) -> dict[str, Any]:
        """拉行情 → 调 AI → 记录节流状态 → 返回中文展示。

        - force=True 绕过节流（价格哨兵早叫 / API 手动触发）；
        - 节流挡住时返回上一次 AI 结果 + 理由前缀提示节流级别；
        - 失败时递增 consec_failures（下次进入 DEGRADED 120s 降频）。
        """
        from ..ai.base import MarketAnalysisResult
        now_ts = int(time.time())

        # 1) 节流决策（哪怕 force=True 也要跑一遍，以持久化 early_wake 统计）
        mark_price = 0.0
        has_pos = False
        entry = 0.0; liq = 0.0
        try:
            pos = await self.broker.get_position(self.config.trading.symbol)
            mark_price = float(getattr(pos, "mark_price", 0.0) or 0.0)
            has_pos = pos.side != PositionSide.FLAT
            entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
            liq = float(getattr(pos, "liquidation_price", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            pos = None  # type: ignore[assignment]
        st_dec = self.state_store.load()
        self.ai_throttler.load_from(st_dec)
        status_raw = (st_dec.get("status") or "") if isinstance(st_dec, dict) else ""
        running = (status_raw == SystemStatus.RUNNING.value)
        risk_d = st_dec.get("risk") or {} if isinstance(st_dec, dict) else {}
        allow = bool(risk_d.get("allow_trading", True))
        dec = self.ai_throttler.should_call_ai(
            now_ts=now_ts,
            system_status_running=running,
            has_position=has_pos,
            allow_trading=allow,
            mark_price=float(mark_price or 2466.0),
            entry_price=entry,
            stop_loss_price=0.0,
            take_profit_price=0.0,
            liquidation_price=liq,
            force=bool(force),
        )
        # 未到时：直接返回上次 AI 结果 + 标注『节流』
        if not dec.should_call:
            last = self._last_ai_block()
            wait = max(dec.next_call_at - now_ts, 0)
            reason_prefix = f"[节流 {dec.level.value} 冷却 {wait}s] {dec.reason} | "
            # 避免重复前缀
            prev_reason = str(last.get("理由") or "")
            if prev_reason.startswith("[节流"):
                import re as _re
                prev_reason = _re.sub(r"^\[节流[^\]]*\][^|]*\|\s*", "", prev_reason)
            last["理由"] = reason_prefix + prev_reason
            last["节流级别"] = dec.level.value
            last["下次调用(秒后)"] = wait
            last["早叫触发(1%波动)"] = dec.early_wake
            last["最近波动(%)"] = round(dec.event_pct, 3)
            return last

        # 2) 拉行情（market_producer → 兜底）
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
        # 3) 调 AI
        ai_ok = True
        try:
            result = await self.ai.analyze_market(md)
        except Exception as e:
            logger.warning("AI 分析失败（回退默认 RANGE）：{}", e)
            ai_ok = False
            result = MarketAnalysisResult(
                market_regime=MarketRegime.RANGE,
                confidence=0,
                reason=f"AI 暂不可用: {e}",
            )
        self._last_ai = result
        self._last_ai_ts = int(time.time() * 1000)
        # 真实价格锚：ticker last / md.close / mark_price → 越准越好
        real_mark = float(md.close or 0.0)
        if real_mark <= 0 and isinstance(getattr(md, "extra", None), dict):
            real_mark = float(md.extra.get("last") or 0.0)  # type: ignore[union-attr]
        if real_mark <= 0 and mark_price > 0:
            real_mark = float(mark_price)
        if real_mark <= 0:
            real_mark = 2466.0
        logger.bind(log_type="trade").info(
            "[AI 决策] 市场状态={} 置信度={} 理由={} (节流={} early_wake={} 波动={:.2f}%)",
            result.market_regime.value, result.confidence, result.reason,
            dec.level.value, dec.early_wake, dec.event_pct,
        )
        # 4) 持久化 last_ai + ai_throttler
        st = self.state_store.load()
        st["last_ai"] = {
            "market_regime": result.market_regime.value,
            "confidence": result.confidence,
            "reason": result.reason,
            "ts": self._last_ai_ts,
            "throttle_level": dec.level.value,
            "early_wake": dec.early_wake,
            "event_pct": dec.event_pct,
        }
        self.ai_throttler.load_from(st)
        self.ai_throttler.record_analyze_outcome(
            now_ts=now_ts, ok=ai_ok, cost_usdt=0.0, mark_price_after=real_mark,
        )
        self.ai_throttler.persist_to(st)
        self.state_store.save(st)
        return {
            "市场状态": _ZH_MARKET.get(result.market_regime, str(result.market_regime)),
            "置信度": result.confidence,
            "理由": result.reason,
            "时间": self._last_ai_ts,
            "节流级别": dec.level.value,
            "下次调用(秒后)": max(dec.next_call_at - now_ts, 0),
            "早叫触发(1%波动)": dec.early_wake,
            "最近波动(%)": round(dec.event_pct, 3),
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

        # 2026-08-30：拿到 symbol 的 MarketSpec → 按 lotSz/szDecimals 重夹一遍 amount（避免 broker 下单还得处理）
        #   Broker 层也有同名规范化兜底，但 Controller 先做一遍可以把「规范化后 sz 不足」提前打日志
        try:
            spec = await self.broker.fetch_market_spec(symbol)
            norm = spec.clamp_sz(amount, is_market=False)
            if norm <= 0:
                return {"status": "REJECTED", "via": "none",
                        "reason": (f"按交易所规则夹后 sz=0（原 amount={amount}, "
                                   f"minSz={spec.min_sz}, lotSz={spec.lot_sz}）"),
                        "order_id": None, "avg_fill_price": 0.0, "qty": 0.0}
            if abs(norm - amount) > 1e-9:
                logger.info("[execute_trade_signal] sz 规范化：{} → {} (lotSz={} decimals={})",
                            amount, norm, spec.lot_sz, spec.sz_decimals)
            amount = norm
        except Exception as e:  # noqa: BLE001
            logger.warning("[execute_trade_signal] MarketSpec sz 规范化失败，按原 amount 继续：{}", e)

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
        """检测日期变化 → 触发 risk.recompute_daily_start_if_suspicious()；
        另外即便日期没切，也照样做一次 daily_start 异常值纠偏（防止 state.json 被旧值污染）。
        返回 True 表示发生了 daily_start_balance 写入 / 或纠正动作。
        """
        import datetime as _dt
        import logging as _logging
        today = today or _dt.date.today().isoformat()
        st = self.state_store.load()
        last_day = st.get("daily_reset_day")
        day_match = bool(last_day) and str(last_day) == str(today)
        bal_total = float((st.get("balance") or {}).get("total", 0.0))
        if bal_total <= 0:
            try:
                import asyncio as _aio
                bal = _aio.get_event_loop().run_until_complete(self.broker.get_balance())
                bal_total = float(getattr(bal, "total", 0.0) or 0)
            except Exception:
                bal_total = 0.0
        reset, reason = self.risk.recompute_daily_start_if_suspicious(
            bal_total,
            daily_reset_day_matches=day_match,
            today_iso=today,
        )
        # 日期没切但纠偏做了（reset=True）也记录；日期切了必然会写 daily_reset_day
        st["daily_reset_day"] = today
        # 风控状态回写（daily_start_balance 若被 reset 时已经在 risk 对象里改了）
        st.setdefault("risk", {}).update(self.risk.to_dict())
        self.state_store.save(st)
        _logger = _logging.getLogger(__name__)
        if reset:
            _logger.info("[日切点] %s", reason)
        else:
            # 日期未切且无需纠偏 → TRACE，避免刷屏
            _logger.debug("[日切点] %s", reason)
        return reset or not day_match
