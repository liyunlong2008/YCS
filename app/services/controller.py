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
        # 2026-08-31 修复 Bug B+C：
        #   B) 用 broker.get_position 实时代替 state_store.position 快照（ShadowBroker 虚拟持仓能真正显现）
        #   C) 把持仓方向 position.side.value 传给 throttler，避免 SHORT 也显示 LONG_HOLD 文案
        import asyncio as _aio_posctl  # noqa: PLC0415
        pos_side_str: str = ""
        pos_has_real: bool = bool(st.get("position") and (st["position"].get("size") or 0) > 0)
        pos_mark_input = float((st.get("position") or {}).get("mark_price", 0.0) or 0.0)
        pos_entry_input = float((st.get("position") or {}).get("entry_price", 0.0) or 0.0)
        pos_liq_input = float((st.get("position") or {}).get("liquidation_price", 0.0) or 0.0)
        current_position_block: dict[str, Any] = {
            "方向": "空仓",
            "数量": 0.0,
            "开仓均价": 0.0,
            "标记价": 0.0,
            "未实现盈亏": 0.0,
            "杠杆": 1,
        }
        try:
            _sym = getattr(self.config.trading, "symbol", None) or "ETH-USDT-SWAP"
            _broker_pos: Position | None = None
            if hasattr(self, "broker") and self.broker is not None:
                async def _fetch_pos(broker_ref, sym_ref):
                    try:
                        return await broker_ref.get_position(sym_ref)
                    except Exception:  # noqa: BLE001
                        return None
                # 同步 get_status_dict 里调异步 broker.get_position：
                #   - 无 running loop：直接 asyncio.run（pytest / CLI 最常见）
                #   - 已有 running loop：先尝试 nest_asyncio.apply() + run_until_complete；
                #     不行就退回 state_store.position 兜底（绝不遗留未 await 协程警告）
                try:
                    _loop = _aio_posctl.get_running_loop()
                except RuntimeError:
                    _loop = None
                if _loop is None:
                    _coro = _fetch_pos(self.broker, _sym)
                    try:
                        _broker_pos = _aio_posctl.run(_coro)
                    except Exception:  # noqa: BLE001
                        _broker_pos = None
                        try:
                            _coro.close()
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    try:
                        import nest_asyncio as _nest  # noqa: PLC0415
                        _nest.apply()
                    except Exception:  # noqa: BLE001
                        pass
                    _coro2 = _fetch_pos(self.broker, _sym)
                    try:
                        _broker_pos = _loop.run_until_complete(_coro2)
                    except Exception:  # noqa: BLE001
                        # 例如 uvloop / 旧 loop 状态不对：走 state_store.position 兜底
                        _broker_pos = None
                        try:
                            _coro2.close()
                        except Exception:  # noqa: BLE001
                            pass
            if _broker_pos is not None:
                _sz = float(getattr(_broker_pos, "size", 0.0) or 0.0)
                _side_obj = getattr(_broker_pos, "side", None)
                _side_raw = getattr(_side_obj, "value", str(_side_obj)) if _side_obj is not None else "FLAT"
                current_position_block = {
                    "方向": _ZH_POSITION.get(_side_obj, _ZH_POSITION.get(type(_side_obj), _side_raw)) if _side_obj else _side_raw,
                    "数量": float(_sz),
                    "开仓均价": float(getattr(_broker_pos, "entry_price", 0.0) or 0.0),
                    "标记价": float(getattr(_broker_pos, "mark_price", 0.0) or 0.0),
                    "未实现盈亏": float(getattr(_broker_pos, "unrealized_pnl", 0.0) or 0.0),
                    "杠杆": int(getattr(_broker_pos, "leverage", 1) or 1),
                    "强平价": float(getattr(_broker_pos, "liquidation_price", 0.0) or 0.0),
                }
                if _sz > 0 and _side_raw != "FLAT":
                    pos_has_real = True
                    pos_side_str = str(_side_raw)
                    pos_mark_input = float(current_position_block["标记价"] or 0.0)
                    pos_entry_input = float(current_position_block["开仓均价"] or 0.0)
                    pos_liq_input = float(current_position_block["强平价"] or 0.0)
        except Exception:  # noqa: BLE001
            # 同步 get_running_loop 报错 / broker 还没装配：退回 state_store.position 兜底
            pass

        try:
            _ = self.ai_throttler.should_call_ai(
                now_ts=now_ts,
                system_status_running=(sys_status == SystemStatus.RUNNING),
                has_position=pos_has_real,
                allow_trading=bool(
                    (self.risk.cooldown_until_ts == 0 or now_ts >= self.risk.cooldown_until_ts)
                    and sys_status == SystemStatus.RUNNING
                ),
                mark_price=pos_mark_input if pos_mark_input > 0 else 2466.0,
                entry_price=pos_entry_input,
                liquidation_price=pos_liq_input,
                position_side=pos_side_str,  # Bug C：SHORT 场景不再写 "LONG_HOLD 持仓中"
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
        # 2026-08-31 修复 Bug A：started_at 空兜底写回 StateStore（recoverer/run.py 漏写时，/api/status 首访也能恢复）
        if started_at_epoch is None:
            started_at_epoch = int(now_ts)
            try:
                if not isinstance(st, dict):
                    st = {}
                st["started_at"] = started_at_epoch
                self.state_store.save(st)
            except Exception:  # noqa: BLE001
                pass
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
                "最近一次风控": self._build_risk_snapshot(
                    pos_mark_input, current_position_block, ai_block
                ),
                # 最近一次通过风控+AI双确认→进入下单流程的时间戳；=0 意味着从未准备下单
                "最近一次交易信号就绪时间戳": int(getattr(self.risk, "last_pass_trade_signal_at", 0) or 0),
            },
            # 2026-08-31 修复 Bug B：新增『当前持仓』= broker.get_position 实时数据。
            #   背景：ShadowBroker 虚拟持仓不落 state_store.position，Dashboard SSR/JS 若只
            #        读 state_store 快照就一直显示空仓；本字段就是 /api/status → JS refresh →
            #        Dashboard 持仓卡 DOM 刷新的唯一真源。
            "当前持仓": current_position_block,
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
    # 2026-08-31：Dashboard『最近一次风控』快照 helper
    #   - 用户反馈："最小名义永远 2.466U，现价 2488 应该 2.5U 左右"
    #     根因：之前直接读 last_verdict.effective_min_notional_usdt（旧价格评估时的快照），
    #           现价变了但 last_verdict 没刷新 → Dashboard 永远显示 2.466U。
    #   - 修复：最小名义 / 现价最小名义（以当前 mark_price 重新计算）都展示，
    #           且缺口本金以"现价联动最小名义"为准。
    # ------------------------------------------------------------------
    def _build_risk_snapshot(
        self,
        current_mark_price: float,
        current_position_block: dict,
        ai_block: Any,
    ) -> dict[str, Any]:
        lv = self.risk.last_verdict
        lv_at = getattr(self.risk, "last_verdict_at", 0) or 0

        # 1) 现价：current_position_block（broker 实时持仓）→ current_mark_price → last_verdict 时价 → 兜底 0
        price_for_min = float(current_position_block.get("标记价", 0.0) or 0.0) if isinstance(current_position_block, dict) else 0.0
        if price_for_min <= 0:
            price_for_min = float(current_mark_price or 0.0)
        # 如果有 last_verdict 且带 suggested_entry_price（暂时 RiskVerdict 里没有，先用老值兜底回推）
        if price_for_min <= 0 and lv is not None:
            # 从 old effective_min_notional 反推 ≈ 旧 entry_price（仅当公式有效时），再按公式不使用
            try:
                from ..broker.base import MarketSpec
                _spec_guess = MarketSpec()
                old_min = float(getattr(lv, "effective_min_notional_usdt", 0.0) or 0.0)
                if old_min > 0:
                    est = old_min / max(float(_spec_guess.min_sz or 0.1) * float(_spec_guess.ct_val or 0.01), 1e-12)
                    if est > 0:
                        pass  # 只估不填：还是等 broker 实时 ticker（避免继续卡旧价）
            except Exception:  # noqa: BLE001
                pass

        # 2) 取 spec（交易规则）+ config_min：优先用 broker 的（但 get_status_dict 是 sync，
        #    没有 broker 就只能用默认 MarketSpec 兜底。run.py 会再用真 broker spec 覆盖，
        #    这里 Dashboard 给一个"现价对照最小名义"方便排查"2.466 不更新"场景）。
        from ..broker.base import MarketSpec  # noqa: PLC0415
        try:
            spec = MarketSpec()
            cfg_rl = getattr(self.config, "risk_limits", None)
            cfg_min = float(getattr(cfg_rl, "min_order_notional_usdt", 0) or 0.0) if cfg_rl else 0.0
        except Exception:  # noqa: BLE001
            spec = MarketSpec()
            cfg_min = 0.0

        old_min = float(getattr(lv, "effective_min_notional_usdt", 0.0) or 0.0) if lv is not None else 0.0
        cur_min = spec.effective_min_notional(price_for_min, cfg_min) if price_for_min > 0 else old_min

        suggested_notional = float(getattr(lv, "suggested_notional_usdt", 0.0) or 0.0) if lv is not None else 0.0
        leverage = int(getattr(lv, "suggested_leverage", 1) or 1) if lv is not None else 1

        # 缺口本金 = (当前联动现价的最小名义 - 建议名义) / 杠杆；仅当"风控拒绝且需要更多本金"时展示
        gap_capital: float | None = None
        if lv is not None and not bool(getattr(lv, "allow", False)) and cur_min > suggested_notional:
            gap_capital = round(max(0.0, (cur_min - suggested_notional) / max(1, leverage)), 4)

        return {
            "时间戳": int(lv_at) if isinstance(lv_at, (int, float)) else 0,
            "结论": (
                "通过" if (lv is not None and bool(getattr(lv, "allow", False)))
                else ("拒绝" if lv is not None else "未执行")
            ),
            "原因": (
                str(getattr(lv, "reason", ""))
                if lv is not None
                else "系统尚未发起风控评估（等下一轮主循环 10s 内）"
            ),
            "建议杠杆(X)": int(getattr(lv, "suggested_leverage", 0) or 0) if lv is not None else None,
            "建议名义价值(USDT)": round(suggested_notional, 4) if lv is not None else None,
            # 最小名义：两条都展示，用户一眼就能判断是不是"没联动现价"
            "最小名义(USDT)": round(old_min, 4) if lv is not None else None,
            "最小名义_按现价(USDT)": round(cur_min, 4) if cur_min > 0 else None,
            "现价参考(USDT)": round(price_for_min, 4) if price_for_min > 0 else None,
            "缺口本金(USDT)": gap_capital,
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
        }

    # ------------------------------------------------------------------
    # 流动性输入抓取（2026-08-31：SLEEP 按流动性不按 UTC；缺任何字段返回 0，节流端自动退化兼容）
    # 2026-08-31 优化：带 30s TTL 实例缓存。背景：
    #   bg_main_loop 的同一 10s 周期里，should_analyze() → 若过节流则 analyze()
    #   两边都调 _fetch_liq_inputs()，导致同一 ticker/ohlcv 在毫秒级间隔里被抓 2 次。
    #   而 1m OHLCV 每 60s 才变一次、买卖价差 30s 内也不会从"极窄"翻成"极宽"，
    #   所以 30s TTL 缓存安全，HOT 档（每 10s 调 AI）节省 50% 的网络 REST 往返。
    # ------------------------------------------------------------------
    async def _fetch_liq_inputs(self, *, force_refresh: bool = False, _ttl_s: int = 30):
        """返回 (bid_price, ask_price, recent_volume_contracts_1m)。失败=0。
        只做 best-effort，不抛异常（AI 节流在输入为 0 时走更宽松的旧判定，不会 SLEEP 误杀）。

        Args:
            force_refresh: True 跳过缓存（手动分析接口要精确最新数据时传）。
            _ttl_s: 缓存有效期秒数（默认 30s，平衡时效性与网络开销）。
        """
        import time as _t
        now = int(_t.time())
        cache = getattr(self, "_liq_inputs_cache", None)
        if (not force_refresh
            and isinstance(cache, tuple) and len(cache) == 4
            and int(cache[0]) + int(_ttl_s) > now):
            # cache = (ts, bid, ask, vol1m)
            return (float(cache[1]), float(cache[2]), float(cache[3]))
        bid = 0.0; ask = 0.0; vol_1m = 0.0
        sym = self.config.trading.symbol
        try:
            # 1) 优先：调 ccxt.fetch_ticker（OKXBroker / 通用 ccxt broker 一般都有）
            bkr = self.broker
            # ShadowBroker：取内部真实 broker
            if hasattr(bkr, "_inner") and getattr(bkr, "_inner", None) is not None:
                bkr = bkr._inner  # type: ignore[assignment]
            tkr: Any = None
            ex: Any = None
            if hasattr(bkr, "_ensure_client") and callable(getattr(bkr, "_ensure_client", None)):
                try:
                    ex = bkr._ensure_client()  # type: ignore[attr-defined]
                    if ex is not None and hasattr(ex, "fetch_ticker"):
                        tkr = await ex.fetch_ticker(sym)
                except Exception:  # noqa: BLE001
                    tkr = None
            if isinstance(tkr, dict):
                bid = float(tkr.get("bid") or 0.0)
                ask = float(tkr.get("ask") or 0.0)
                # 2) 1m 成交量：优先 fetch_ohlcv(1m, limit=1) 最准；取不到就用 ticker 24h 量/1440 粗估
                try:
                    if ex is not None and hasattr(ex, "fetch_ohlcv"):
                        bars = await ex.fetch_ohlcv(sym, "1m", limit=1)
                        if bars and len(bars) > 0 and len(bars[0]) >= 6:
                            vol_1m = float(bars[0][5] or 0.0)
                except Exception:  # noqa: BLE001
                    vol_1m = 0.0
                if vol_1m <= 0:
                    # 兜底：24h 量估算 1m 量（仅用于"量足/量低"打分，误差影响不大）
                    vol_24h = float(tkr.get("baseVolume") or 0.0)
                    if vol_24h > 0:
                        vol_1m = max(1.0, vol_24h / 1440.0)
        except Exception:  # noqa: BLE001
            pass
        bid = float(bid); ask = float(ask); vol_1m = float(vol_1m)
        # 写缓存（即便是 0 结果也缓存，避免网络重试刷屏）
        try:
            self._liq_inputs_cache = (now, bid, ask, vol_1m)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return bid, ask, vol_1m

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

        # 2026-08-31：抓取流动性输入（SLEEP 按价差+量+波动判断；不传则退化更宽松，不误杀）
        _bid, _ask, _vol1m = 0.0, 0.0, 0.0
        try:
            _bid, _ask, _vol1m = await self._fetch_liq_inputs()
        except Exception:  # noqa: BLE001
            pass
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
            bid_price=_bid,
            ask_price=_ask,
            recent_volume_contracts=_vol1m,
        )
        # 持久化：哪怕本轮不调 AI，价格哨兵的 last_event_pct / last_event_at / level 也要存
        st2 = self.state_store.load()
        self.ai_throttler.persist_to(st2)
        self.state_store.save(st2)
        return dec

    # ------------------------------------------------------------------
    async def analyze(
        self,
        force: bool = False,
        *,
        # 2026-08-31 性能优化：bg_main_loop 同一 10s 轮次已经抓过 position 了，直接透传省一次 broker REST。
        #   传 None / 0 / False = 未知，内部自动查 broker（兼容旧调用方 / 手动触发 API）。
        preloaded_pos: object = None,
        preloaded_mark_price: float = 0.0,
        preloaded_entry_price: float = 0.0,
        preloaded_liq_price: float = 0.0,
        preloaded_has_position: Optional[bool] = None,
    ) -> dict[str, Any]:
        """拉行情 → 调 AI → 记录节流状态 → 返回中文展示。

        - force=True 绕过节流（价格哨兵早叫 / API 手动触发）；
        - 节流挡住时返回上一次 AI 结果 + 理由前缀提示节流级别；
        - 失败时递增 consec_failures（下次进入 DEGRADED 120s 降频）。
        """
        from ..ai.base import MarketAnalysisResult
        now_ts = int(time.time())

        # 1) 节流决策（哪怕 force=True 也要跑一遍，以持久化 early_wake 统计）
        # 2026-08-31 优化：bg_main_loop 已经抓过 pos/mark/entry/liq → 直接复用，避免同 10s 重复 REST
        mark_price = 0.0
        has_pos = False
        entry = 0.0; liq = 0.0
        pos = preloaded_pos
        if preloaded_has_position is not None:
            has_pos = bool(preloaded_has_position)
        if float(preloaded_mark_price or 0.0) > 0:
            mark_price = float(preloaded_mark_price)
        if float(preloaded_entry_price or 0.0) > 0:
            entry = float(preloaded_entry_price)
        if float(preloaded_liq_price or 0.0) > 0:
            liq = float(preloaded_liq_price)
        # 预加载的数据不全时，才真正调 broker.get_position()（手动 API / 利润保护平仓后的数据刷新场景）
        _need_broker_pos = (
            (pos is None)
            or (preloaded_has_position is None)
            or (mark_price <= 0)
        )
        if _need_broker_pos:
            try:
                pos = await self.broker.get_position(self.config.trading.symbol)
                mark_price = float(getattr(pos, "mark_price", 0.0) or 0.0)
                from ..core.constants import PositionSide as _PS2  # noqa: PLC0415
                has_pos = (pos.side != _PS2.FLAT)
                if entry <= 0:
                    entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if liq <= 0:
                    liq = float(getattr(pos, "liquidation_price", 0.0) or 0.0)
            except Exception:  # noqa: BLE001
                pos = None  # type: ignore[assignment]
        st_dec = self.state_store.load()
        self.ai_throttler.load_from(st_dec)
        status_raw = (st_dec.get("status") or "") if isinstance(st_dec, dict) else ""
        running = (status_raw == SystemStatus.RUNNING.value)
        risk_d = st_dec.get("risk") or {} if isinstance(st_dec, dict) else {}
        allow = bool(risk_d.get("allow_trading", True))
        # 2026-08-31：流动性输入（节流根据流动性不按 UTC0-6 硬睡）
        _bid, _ask, _vol1m = 0.0, 0.0, 0.0
        try:
            _bid, _ask, _vol1m = await self._fetch_liq_inputs()
        except Exception:  # noqa: BLE001
            pass
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
            bid_price=_bid,
            ask_price=_ask,
            recent_volume_contracts=_vol1m,
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
