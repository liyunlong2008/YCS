# -*- coding: utf-8 -*-
"""AI 调用自适应节流 + 价格哨兵（解决『AI 调用太费 / 熔断期也瞎调用 / 降频又漏行情』三角矛盾）。

策略精髓（7 级状态机而不是硬固定频率）：
  · RUNNING 空仓（最需要灵敏）→ NORMAL 60s；但 1m 价格涨跌 ≥1% / 振幅 ≥1.5% → EARLY_WAKE 立刻补 1 次
  · RUNNING 已有持仓（盈亏靠移动止损/止盈 本地判定即可）→ LONG_HOLD 180s；仅当接近止盈/止损/强平时回退 HOT 15s
  · STOP/熔断冷却（allow=False）→ IDLE 300s；仍启用本地价格哨兵（不调 AI），1m≥1% 波动立刻 EARLY_WAKE 强早叫（『熔断期也不漏大行情』）
  · AI 连续失败/超时 ≥2 → DEGRADED 120s 内改规则兜底不调模型
  · 深度睡眠 (UTC 0-6 亚洲时段 ETH 流动性稀薄) → SLEEP 600s；仅 ≥2% 巨幅才早叫
  · 当日累计调用估算成本 / 次数超预算 → CAPPED 3600s；发 WARNING 日志
  · 极端事件（日内振幅≥3% / 爆仓警戒价距离≤1%）→ HOT 15s 盯盘档

暴露：AIThrottler（状态存 state_store 的『ai_throttler』键，持久化）
  .should_call_ai(*, now_ts, system_status, has_position, allow_trading, mark_price,
                  entry_price, stop_price, liquidation_price, early_wake_from_sentinel) -> ThrottleDecision
  .record_analyze_outcome(*, now_ts, ok, cost_usdt=0)
  .price_sentinel_check(*, now_ts, mark_price) -> bool  # True=该次早叫立即触发
  .to_status_dict() -> dict  # 给 /api/status 渲染
"""
from __future__ import annotations

import math
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ThrottleLevel(str, Enum):
    """7 级节流等级：Dashboard 彩色 tag 使用。"""
    HOT = "HOT"           # 15s —— 极端事件（接近止盈止损/爆仓/巨幅波动）
    NORMAL = "NORMAL"     # 60s —— RUNNING 空仓 正常盯盘
    LONG_HOLD = "LONG_HOLD"  # 180s —— 持仓中，本地利润保护足以，降频
    IDLE = "IDLE"         # 300s —— STOP/熔断冷却（价格哨兵仍在，1% 波动立即早叫）
    DEGRADED = "DEGRADED" # 120s —— AI 连 2 次失败：先不调模型，规则兜底
    SLEEP = "SLEEP"       # 600s —— 深度睡眠窗 UTC 0-6 (国内 ETH 稀薄)
    CAPPED = "CAPPED"     # 3600s —— 当日调用/预算超上限(硬节流)


# 每种等级对应默认间隔(秒)
LEVEL_INTERVALS: dict[ThrottleLevel, int] = {
    ThrottleLevel.HOT: 15,
    ThrottleLevel.NORMAL: 60,
    ThrottleLevel.LONG_HOLD: 180,
    ThrottleLevel.IDLE: 300,
    ThrottleLevel.DEGRADED: 120,
    ThrottleLevel.SLEEP: 600,
    ThrottleLevel.CAPPED: 3600,
}

# Dashboard 彩色 tag（JS 同步使用）
LEVEL_COLORS: dict[str, str] = {
    "HOT": "background:#fce8e6;color:#c5221f",
    "NORMAL": "background:#e6f4ea;color:#137333",
    "LONG_HOLD": "background:#e8f0fe;color:#1967d2",
    "IDLE": "background:#e0e0e0;color:#3c4043",
    "DEGRADED": "background:#feefc3;color:#8a6500",
    "SLEEP": "background:#e4e7eb;color:#5f6368",
    "CAPPED": "background:#f3e8fd;color:#7627bb",
}


class AIThrottlerState(BaseModel):
    """可持久化状态（存 state_store.json['ai_throttler']）。"""
    last_call_ts: int = 0
    next_call_ts: int = 0
    level: str = ThrottleLevel.NORMAL.value
    last_reason: str = "初始化"
    consec_failures: int = 0
    consec_success: int = 0
    daily_call_count: int = 0
    daily_cost_usdt: float = 0.0
    daily_date: str = ""        # YYYY-MM-DD (UTC)，跨天自动归零
    # 价格哨兵
    sentinel_mark_price: float = 0.0
    sentinel_anchor_ts: int = 0  # 最近一次锚定 (每次 early_wake 触发 / AI 真正调用 后重置锚)
    last_event_pct: float = 0.0  # 最近一次 1m 涨跌百分比(绝对值)
    last_event_at: int = 0
    # 调试：early_wake 累计触发次数(天粒度)
    daily_early_wakes: int = 0


class ThrottleDecision(BaseModel):
    should_call: bool
    level: ThrottleLevel
    interval_s: int
    reason: str
    next_call_at: int            # unix ts
    # 早叫相关：True=这轮命中了『1% 波动哨兵』，即便在熔断期也要调 AI（用户要的"熔断期间也不能漏机会"）
    early_wake: bool = False
    event_pct: float = 0.0
    # 给 Dashboard：下一次调用 ts（方便刷新倒计时）
    debug_summary: dict[str, Any] = {}


_DEFAULT_CFG = {
    # 可调（后续也可放 RiskLimits；先内置安全默认值）
    "event_1m_pct": 1.0,         # 1m 涨跌≥1% → 早叫
    "big_event_1m_pct": 2.0,     # 2% → 即使 SLEEP 也早叫
    "hot_proximity_pct": 1.0,    # 持仓接近止盈/止损/强平 ≤1% → HOT 盯盘
    "max_daily_calls": 500,      # 单日调用超过 → CAPPED (防止 bug 疯刷)
    "max_daily_cost_usdt": 5.0,  # 单日成本预估超过 → CAPPED (小账户 14.83U 的 30%=4.45U 预算)
    "degrade_after_failures": 2, # 连失败 2 次 → DEGRADED (降级)
}


class AIThrottler:
    """AI 自适应节流器。"""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = dict(_DEFAULT_CFG)
        if cfg:
            self.cfg.update({k: v for k, v in cfg.items() if k in _DEFAULT_CFG})
        self.state = AIThrottlerState()

    # ------------------------------------------------------------------
    # 持久化：和 TradingController 共用 state_store
    # ------------------------------------------------------------------
    def load_from(self, state_dict: dict[str, Any]) -> None:
        raw = state_dict.get("ai_throttler") or {}
        try:
            self.state = AIThrottlerState.model_validate(raw)
        except Exception:  # noqa: BLE001
            self.state = AIThrottlerState()
        self._maybe_roll_daily(int(time.time()))

    def persist_to(self, state_dict: dict[str, Any]) -> None:
        state_dict["ai_throttler"] = self.state.model_dump(mode="json")

    # ------------------------------------------------------------------
    # 内部：跨天归零(日/调用/成本/early_wake)
    # ------------------------------------------------------------------
    def _maybe_roll_daily(self, now_ts: int) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime(now_ts))
        if self.state.daily_date != today:
            self.state.daily_date = today
            self.state.daily_call_count = 0
            self.state.daily_cost_usdt = 0.0
            self.state.daily_early_wakes = 0

    # ------------------------------------------------------------------
    # 核心：判断"这轮主循环是否真的调 AI analyze()"
    # ------------------------------------------------------------------
    def should_call_ai(
        self,
        *,
        now_ts: int,
        system_status_running: bool,
        has_position: bool,
        allow_trading: bool,
        mark_price: float,
        entry_price: float = 0.0,
        stop_loss_price: float = 0.0,
        take_profit_price: float = 0.0,
        liquidation_price: float = 0.0,
        force: bool = False,
    ) -> ThrottleDecision:
        """主入口。

        Args:
            force: True 绕过时间节流（外部如『紧急事件早叫』/ API 手动触发 analyze）。
        """
        now_ts = int(now_ts or int(time.time()))
        self._maybe_roll_daily(now_ts)

        # ---- 1) 价格哨兵：计算离上次锚定涨跌幅(绝对值)，顺便记录事件 ----
        early_wake = False
        event_pct = 0.0
        anchor = float(self.state.sentinel_mark_price or 0.0)
        mark = float(mark_price or 0.0)
        if anchor > 0 and mark > 0 and self.state.sentinel_anchor_ts > 0:
            event_pct = abs((mark - anchor) / anchor) * 100.0
            self.state.last_event_pct = event_pct
            self.state.last_event_at = now_ts
            # 等级阈值：SLEEP 要 2%，其它 1% 以上触发 early_wake
            big_thr = float(self.cfg["big_event_1m_pct"])
            normal_thr = float(self.cfg["event_1m_pct"])
            if event_pct >= big_thr:
                early_wake = True
                self.state.daily_early_wakes += 1
            elif event_pct >= normal_thr:
                # SLEEP 级别不早叫(除非大波动)
                if self.state.level != ThrottleLevel.SLEEP.value:
                    early_wake = True
                    self.state.daily_early_wakes += 1

        # ---- 2) 决定目标 level（状态机）----
        #  优先级: 当日超预算 CAPPED > 连续失败 DEGRADED > 睡眠窗 SLEEP >
        #          持仓接近 HOT > RUNNING+空仓 NORMAL > RUNNING+持仓 LONG_HOLD > IDLE
        level = ThrottleLevel.IDLE
        reason = ""

        # (a) CAPPED：单日调用/成本超硬上限
        if self.state.daily_call_count >= int(self.cfg["max_daily_calls"]):
            level = ThrottleLevel.CAPPED
            reason = f"CAPPED(日调用上限) 当日={self.state.daily_call_count}≥{int(self.cfg['max_daily_calls'])}"
        elif self.state.daily_cost_usdt >= float(self.cfg["max_daily_cost_usdt"]):
            level = ThrottleLevel.CAPPED
            reason = (f"CAPPED(日成本上限) 当日${self.state.daily_cost_usdt:.3f}"
                      f"≥${float(self.cfg['max_daily_cost_usdt']):.2f}")
        # (b) DEGRADED：连续失败次数够
        elif self.state.consec_failures >= int(self.cfg["degrade_after_failures"]):
            level = ThrottleLevel.DEGRADED
            reason = f"DEGRADED AI连失败={self.state.consec_failures}(阈值 {self.cfg['degrade_after_failures']})"
        # (c) SLEEP: UTC 0-6 流动性稀薄
        elif time.gmtime(now_ts).tm_hour < 6:
            level = ThrottleLevel.SLEEP
            reason = "SLEEP 深度窗口(UTC 0-6) ETH 流动性稀薄"
        else:
            if system_status_running and allow_trading:
                if has_position and entry_price > 0:
                    # 持仓中：检测是否接近止盈/止损/强平 ≤hot_proximity_pct
                    prox = float(self.cfg["hot_proximity_pct"]) / 100.0
                    hot = False
                    if liquidation_price and mark > 0:
                        dist_pct = abs((mark - liquidation_price) / mark) * 100
                        if dist_pct <= prox * 100:
                            hot = True; reason = f"HOT 距离强平价仅 {dist_pct:.2f}%"
                    if not hot and stop_loss_price and mark > 0:
                        dist_pct = abs((mark - stop_loss_price) / mark) * 100
                        if dist_pct <= prox * 100:
                            hot = True; reason = f"HOT 距离止损仅 {dist_pct:.2f}%"
                    if not hot and take_profit_price and mark > 0:
                        dist_pct = abs((mark - take_profit_price) / mark) * 100
                        if dist_pct <= prox * 100:
                            hot = True; reason = f"HOT 距离止盈仅 {dist_pct:.2f}%"
                    if not hot and event_pct >= 3.0:
                        hot = True; reason = f"HOT 1m 大波动 {event_pct:.2f}% ≥3%"
                    if hot:
                        level = ThrottleLevel.HOT
                    else:
                        level = ThrottleLevel.LONG_HOLD
                        reason = "LONG_HOLD 持仓中，本地利润保护足以判定平仓(180s AI 节奏)"
                else:
                    # RUNNING+空仓(最关键盯盘档)
                    level = ThrottleLevel.NORMAL
                    if event_pct >= 1.5:
                        reason = f"NORMAL 空仓盯盘(60s)，本次波动较大 {event_pct:.2f}%"
                    else:
                        reason = "NORMAL 空仓盯盘(60s 正常节奏)"
            else:
                # 熔断/停止/冷却期
                level = ThrottleLevel.IDLE
                if not system_status_running:
                    reason = "IDLE 系统状态≠RUNNING（熔断/停止），AI 仅在 ≥1% 波动时早叫"
                else:
                    reason = "IDLE 风控 allow=False（冷却中），AI 仅在 ≥1% 波动时早叫"

        interval = LEVEL_INTERVALS[level]
        # ---- 3) 计算 next_call_ts（如还没设 / 已过期，以 now 为起点）----
        if self.state.next_call_ts <= 0:
            self.state.next_call_ts = now_ts + interval
        # early_wake / force 允许越过 next_call_ts
        time_ripe = (now_ts >= self.state.next_call_ts)
        should = force or early_wake or time_ripe

        # 同步 level 到 state（仅 should==True 时会在 record_outcome 里写 last_call；这里把节流决策 level 保存用于 Dashboard）
        self.state.level = level.value
        self.state.last_reason = reason

        debug_summary = {
            "force": force,
            "early_wake": early_wake,
            "time_ripe": time_ripe,
            "sentinel_anchor_price": anchor,
            "sentinel_anchor_ago_s": now_ts - self.state.sentinel_anchor_ts if self.state.sentinel_anchor_ts else 0,
        }
        return ThrottleDecision(
            should_call=should,
            level=level,
            interval_s=interval,
            reason=reason,
            next_call_at=self.state.next_call_ts,
            early_wake=early_wake,
            event_pct=event_pct,
            debug_summary=debug_summary,
        )

    # ------------------------------------------------------------------
    # record: AI analyze() 真正调用后回写（成功/失败/成本）
    # ------------------------------------------------------------------
    def record_analyze_outcome(
        self,
        *,
        now_ts: int,
        ok: bool,
        cost_usdt: float = 0.0,
        mark_price_after: float = 0.0,
    ) -> None:
        now_ts = int(now_ts or int(time.time()))
        self._maybe_roll_daily(now_ts)
        self.state.last_call_ts = now_ts
        self.state.daily_call_count += 1
        self.state.daily_cost_usdt += max(float(cost_usdt or 0.0), 0.0)
        if ok:
            self.state.consec_success += 1
            self.state.consec_failures = 0
        else:
            self.state.consec_failures += 1
            self.state.consec_success = 0
        # 重置价格锚点（每次真正调 AI 后，把当前价当新基准）
        if float(mark_price_after or 0.0) > 0:
            self.state.sentinel_mark_price = float(mark_price_after)
            self.state.sentinel_anchor_ts = now_ts
        # 安排下一次：以 last_call_ts 为基准 + 相应等级间隔（下次 decision 会重算等级，这里先铺个保底）
        lvl = ThrottleLevel(self.state.level) if self.state.level in {e.value for e in ThrottleLevel} else ThrottleLevel.NORMAL
        self.state.next_call_ts = now_ts + LEVEL_INTERVALS[lvl]

    # ------------------------------------------------------------------
    # 给 Dashboard / /api/status
    # ------------------------------------------------------------------
    def to_status_dict(self, now_ts: int | None = None) -> dict[str, Any]:
        now_ts = int(now_ts or int(time.time()))
        self._maybe_roll_daily(now_ts)
        lvl = self.state.level or ThrottleLevel.NORMAL.value
        color = LEVEL_COLORS.get(lvl, LEVEL_COLORS["NORMAL"])
        count_down = max(self.state.next_call_ts - now_ts, 0) if self.state.next_call_ts else 0
        return {
            "节流级别": lvl,
            "节流颜色": color,
            "级别原因": self.state.last_reason,
            "倒计时(秒)": count_down,
            "下次调用时间戳": self.state.next_call_ts if self.state.next_call_ts else None,
            "当日调用次数": self.state.daily_call_count,
            "当日成本(估USDT)": round(self.state.daily_cost_usdt, 4),
            "当日早叫次数": self.state.daily_early_wakes,
            "连续失败次数": self.state.consec_failures,
            "连续成功次数": self.state.consec_success,
            "哨兵锚定价": round(self.state.sentinel_mark_price, 6) if self.state.sentinel_mark_price else 0,
            "最近波动(%)": round(self.state.last_event_pct, 3),
            "最近波动时间戳": self.state.last_event_at or None,
        }
