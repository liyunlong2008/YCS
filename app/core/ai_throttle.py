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

# 动态倍率上下限（= 极端行情不下探到 1s 轮询 API；横盘时不睡到 30min 漏消息）
DYN_MULT_MAX = 2.0
DYN_MULT_MIN = 0.25
# 波动率参考阈值：event 等于这个值时倍率恰好 =1（=基础档节奏）
DYN_VOL_PIVOT_PCT = 1.0

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
    # 2026-08-31 流动性驱动 SLEEP：最近一次评估的流动性快照（给 Dashboard）
    last_spread_pct: float = 0.0   # 最近一次 (ask-bid)/mid * 100
    last_volume_1m: float = 0.0    # 最近 1m 合约张
    last_liq_score: int = 0        # 最近一次综合分（>=sleep_liq_score_thr → SLEEP）


class ThrottleDecision(BaseModel):
    should_call: bool
    level: ThrottleLevel
    interval_s: int
    reason: str
    next_call_at: int            # unix ts
    # 早叫相关：True=这轮命中了『1% 波动哨兵』，即便在熔断期也要调 AI（用户要的"熔断期间也不能漏机会"）
    early_wake: bool = False
    event_pct: float = 0.0
    # 2026-08-31 动态行情驱动：基础档间隔 / 波动率 / 最终倍率 / 最终动态间隔
    base_interval_s: int = 0
    dyn_mult: float = 1.0
    # 给 Dashboard：下一次调用 ts（方便刷新倒计时）
    debug_summary: dict[str, Any] = {}


_DEFAULT_CFG = {
    # 可调（后续也可放 RiskLimits；先内置安全默认值）
    "event_1m_pct": 1.0,         # 1m 涨跌≥1% → 早叫
    "big_event_1m_pct": 2.0,     # 2% → 即使默认 SLEEP 也早叫(+额外更激进动态倍率)
    "sleep_wake_pct": 1.0,       # 2026-08-31 新增：SLEEP 窗的早叫阈值(原固定=big_event_1m_pct → 会错过 1.8% 急跌)
    "hot_proximity_pct": 1.0,    # 持仓接近止盈/止损/强平 ≤1% → HOT 盯盘
    "max_daily_calls": 500,      # 单日调用超过 → CAPPED (防止 bug 疯刷)
    "max_daily_cost_usdt": 5.0,  # 单日成本预估超过 → CAPPED (小账户 14.83U 的 30%=4.45U 预算)
    "degrade_after_failures": 2, # 连失败 2 次 → DEGRADED (降级)
    "dyn_mult_max": 2.0,         # 横盘倍率上限（最长 ≤ 2× 基础档）
    "dyn_mult_min": 0.25,        # 高波动倍率下限（最短 ≥ 25% 基础档）
    "dyn_vol_pivot_pct": 1.0,    # 波动率=此值时 倍率=1（=基础档节奏）
    # 2026-08-31 新增：SLEEP 根据『流动性』不按 UTC 时间（用户原话：「节流应该根据行情流动性 而不是utc0-6」）
    #   公式：综合分 = 价差% + 量罚分 + 波动罚分；分越高=流动性越差；> sleep_score_thr → SLEEP
    "liq_spread_bad_pct": 0.35,  # 买卖价差≥0.35% → 判定"价差宽"（=流动性差信号）
    "liq_spread_good_pct": 0.10, # 价差≤0.10% → 判定"价差窄"（=流动性充足，直接否决 SLEEP）
    "liq_vol_low_contracts": 150,# 最近 1m 成交量<150 张 → 量罚分开启（ETH 永续 1m 150 张≈没什么人交易）
    "liq_vol_ok_contracts": 1500,# 量≥1500 张 → 量充足（否决 SLEEP）
    "sleep_liq_score_thr": 60,   # 综合分≥60 分 → SLEEP（默认=价差大+量低+0波动≈必中）
    "sleep_utc06_score_bonus": 25,  # UTC 0-6 亚洲深夜：额外 +25 分（更容易睡，但仍必须看量/价差，不是硬睡）
    #   早叫阈值的流动性豁免（流动性充足时，即便 UTC 0-6 也按 NORMAL 档的 normal_thr 触发早叫）
    "liq_flat_event_pct": 0.05,  # 1m 波动≤0.05% → 流动性"没行情"罚分 20
}


class AIThrottler:
    """AI 自适应节流器。"""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = dict(_DEFAULT_CFG)
        if cfg:
            self.cfg.update({k: v for k, v in cfg.items() if k in _DEFAULT_CFG})
        self.state = AIThrottlerState()

    # ------------------------------------------------------------------
    # 内部 helper：根据行情波动率(event_pct)算出「动态倍率 × 基础档 = 真正间隔」
    #   - 波动越大，倍率越小 → 间隔越短（盯盘更紧）
    #   - 横盘 0.2% 以下倍率越大 → 间隔越长（省 AI 成本）
    #   - CAPPED 档不参与倍率计算(硬性日成本上限)
    # ------------------------------------------------------------------
    def _dyn_mult(self, event_pct: float) -> tuple[float, str]:
        """返回 (multiplier, 简短中文解释)。"""
        pivot = float(self.cfg["dyn_vol_pivot_pct"])
        mx = float(self.cfg["dyn_mult_max"])
        mn = float(self.cfg["dyn_mult_min"])
        ev = max(0.0, float(event_pct or 0.0))
        if ev <= 0:  # 没参考波动 → 横盘拉满
            return mx, f"无锚定波动→横盘倍率×{mx:.2f}"
        # 反比例：倍率 ≈ pivot / ev（ev=pivot 时恰好 =1）；再 clamp 到 [mn, mx]
        raw = pivot / ev if ev > 1e-9 else mx
        mult = max(mn, min(mx, raw))
        # 中文解释：给 Dashboard/日志一眼看懂
        if ev >= float(self.cfg["dyn_vol_pivot_pct"]):
            kind = "高波动缩短"
        elif ev <= float(self.cfg["event_1m_pct"]) * 0.5:
            kind = "横盘拉长"
        else:
            kind = "常态锚定"
        return mult, f"{kind}(波动{ev:.2f}% 倍率×{mult:.2f})"

    # ------------------------------------------------------------------
    # 2026-08-31 流动性打分：SLEEP 根据『真实行情流动』不按 UTC 0-6 硬切。
    #   得分越高 = 流动性越差。UTC 0-6 只是"更容易睡加分"，不是硬睡。
    #
    #   返回 (score, explain_str, force_no_sleep)
    #     force_no_sleep: True → 直接否决 SLEEP（例如价差很窄+量足 → 有行情）
    # ------------------------------------------------------------------
    def _liq_score(
        self,
        *,
        bid: float,
        ask: float,
        mark: float,
        recent_volume: float,
        event_pct: float,
        now_ts: int,
    ) -> tuple[int, str, bool]:
        cfg = self.cfg
        spread_pct = 0.0
        mid = (float(bid or 0.0) + float(ask or 0.0)) / 2.0
        if mid > 0 and float(ask or 0.0) > 0 and float(bid or 0.0) > 0:
            spread_pct = abs(float(ask) - float(bid)) / mid * 100.0
        score = 0
        parts: list[str] = []
        # 1) 价差
        good_sp = float(cfg["liq_spread_good_pct"])
        bad_sp = float(cfg["liq_spread_bad_pct"])
        force_no_sleep = False
        if spread_pct > 0:
            if spread_pct <= good_sp:
                # 价差极窄 → 流动性充足（直接否决 SLEEP）
                parts.append(f"价差窄({spread_pct:.2f}%≤{good_sp:.2f}%，流动性充足)")
                force_no_sleep = True
            elif spread_pct >= bad_sp:
                # 价差超坏线 → 罚 40 分（重罚）
                score += 40
                parts.append(f"价差宽({spread_pct:.2f}%≥{bad_sp:.2f}%，流动性差，+40)")
            else:
                # 介于 good~bad：按线性映射 10~30 分
                ratio = (spread_pct - good_sp) / max(1e-9, (bad_sp - good_sp))
                add = int(10 + ratio * 20)
                score += add
                parts.append(f"价差中等({spread_pct:.2f}%，+{add})")
        # 2) 量
        vol = max(0.0, float(recent_volume or 0.0))
        vol_low = float(cfg["liq_vol_low_contracts"])
        vol_ok = float(cfg["liq_vol_ok_contracts"])
        if vol > 0:
            if vol >= vol_ok:
                parts.append(f"量足(1m={vol:.0f}张≥{vol_ok:.0f})→流动性充足")
                force_no_sleep = True  # 量够就不睡（有交易就可能有行情）
            elif vol <= vol_low:
                # 量没到最低预期 → 罚 30 分
                score += 30
                parts.append(f"量低(1m={vol:.0f}张≤{vol_low:.0f})，+30")
            else:
                ratio = (vol_ok - vol) / max(1e-9, (vol_ok - vol_low))
                add = int(5 + ratio * 20)
                score += add
                parts.append(f"量中等(1m={vol:.0f}张)，+{add}")
        else:
            # 量缺失（没传）→ 不加分，也不否决（避免没数据时误杀/误睡）
            parts.append("成交量未知(未提供)，不参与流动性判档")
        # 3) 波动（横盘=没行情，加 20；有波动不加，波动超大再减）
        flat_pct = float(cfg["liq_flat_event_pct"])
        ev = max(0.0, float(event_pct or 0.0))
        if ev <= flat_pct:
            score += 20
            parts.append(f"横盘(波动{ev:.2f}%≤{flat_pct:.2f})，+20")
        elif ev >= 0.3:
            # 有行情，给 10 分"拉回来"（防止 0 波动的判罚叠加过度）
            score = max(0, score - 10)
            parts.append(f"有行情(波动{ev:.2f}%≥0.3%)，-10 不倾向于睡")
        # 4) UTC 0-6：亚洲深夜的"助眠加成"但不硬睡
        utc_h = time.gmtime(now_ts).tm_hour
        if utc_h < 6:
            bonus = int(float(cfg["sleep_utc06_score_bonus"]))
            score += bonus
            parts.append(f"UTC{utc_h:02d}:00(亚洲深夜习惯清淡)，+{bonus} 加成")
        else:
            parts.append(f"UTC{utc_h:02d}:00（非深夜窗口，不加成）")
        explain = f"流动性评分={score}：" + "；".join(parts)
        # 保存快照给 Dashboard
        self.state.last_spread_pct = round(spread_pct, 4)
        self.state.last_volume_1m = round(vol, 4)
        self.state.last_liq_score = int(score)
        return int(score), explain, bool(force_no_sleep)

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
        position_side: str = "",
        # 2026-08-31 流动性驱动 SLEEP：可空（旧调用方不崩）。传了就真实参与打分。
        bid_price: float = 0.0,
        ask_price: float = 0.0,
        recent_volume_contracts: float = 0.0,
    ) -> ThrottleDecision:
        """主入口。

        Args:
            force: True 绕过时间节流（外部如『紧急事件早叫』/ API 手动触发 analyze）。
            bid_price/ask_price: 最新一档买一/卖一价，用于计算买卖价差%（流动性判断）。
            recent_volume_contracts: 最近 1 分钟的合约成交量（张）。
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
            # 2026-08-31 升级：SLEEP / 其他档 分三级阈值
            #   · ≥big_event_1m_pct(默认 2%)：任何档早叫(最激进)
            #   · ≥sleep_wake_pct(默认 1%)：仅 SLEEP 窗（= SLEEP 档位命中时）早叫(解决旧 SLEEP 只认 2% → 1.8% 漏行情)
            #   · ≥event_1m_pct(默认 1%)：非 SLEEP 档早叫
            big_thr = float(self.cfg["big_event_1m_pct"])
            sleep_thr = float(self.cfg["sleep_wake_pct"])
            normal_thr = float(self.cfg["event_1m_pct"])
            in_sleep = (self.state.level == ThrottleLevel.SLEEP.value)
            if event_pct >= big_thr:
                early_wake = True
                self.state.daily_early_wakes += 1
            elif in_sleep and event_pct >= sleep_thr:
                # SLEEP 档波动达标：立刻早叫
                early_wake = True
                self.state.daily_early_wakes += 1
            elif (not in_sleep) and event_pct >= normal_thr:
                early_wake = True
                self.state.daily_early_wakes += 1

        # ---- 2) 决定目标 level（状态机）----
        #  优先级: 当日超预算 CAPPED > 连续失败 DEGRADED > 流动性真差 SLEEP >
        #          持仓接近 HOT > RUNNING+空仓 NORMAL > RUNNING+持仓 LONG_HOLD > IDLE
        level = ThrottleLevel.IDLE
        reason = ""

        # 2026-08-31 先算一次流动性打分（给后续 (c) SLEEP 判定 & to_status_dict 展示 & reason 中文说明）
        liq_score, liq_reason, liq_force_no_sleep = self._liq_score(
            bid=float(bid_price or 0.0),
            ask=float(ask_price or 0.0),
            mark=mark,
            recent_volume=float(recent_volume_contracts or 0.0),
            event_pct=event_pct,
            now_ts=now_ts,
        )
        _liq_prefix = ""   # NORMAL/持仓档会拼此前缀（证明按流动性判、没硬按 UTC 睡）

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
        # (c) SLEEP: 2026-08-31 根据『流动性差不差』判断；UTC 0-6 只做加分项，不再硬切。
        #     新规则：打分 >= sleep_liq_score_thr(默认 60) 且 没被 force_no_sleep 否决 → SLEEP
        #     旧调用方不传 bid/ask/volume 时：bid/ask=0 → spread 部分不贡献；vol=0 也不贡献
        #          → liq 打分结果就只有 "波动≤0.05% → +20；UTC0-6 +25；其它 0"
        #          → 若 bid/ask 没传，一般会退化成：波动≥0.3% 时 -10，总共最多 35 分<60 → 不会 SLEEP（更宽松）
        elif liq_force_no_sleep:
            # 流动性充足（价差极窄 或 量足） → 坚决不睡（用户：按流动性不按 UTC）
            #   进入后面 RUNNING/NORMAL 分支
            reason = ""  # 等下面再填；这里不占 reason
            level = ThrottleLevel.IDLE  # 占位 → 下方 RUNNING 分支会重写
            _force_no_sleep = True
            _liq_prefix = "流动性充足→按行情不睡(不硬按UTC0-6)；"
        else:
            thr = int(float(self.cfg["sleep_liq_score_thr"]))
            if liq_score >= thr:
                level = ThrottleLevel.SLEEP
                reason = f"SLEEP 流动性差（{liq_reason}，阈值≥{thr}分）；不再硬按 UTC 0-6"
            else:
                _liq_prefix = f"流动性可({liq_score}分<{thr}分SLEEP阈值)；"
                reason = ""  # 占位
                level = ThrottleLevel.IDLE
        # (c) 结束后：如果占位到 IDLE 但实际不应该 IDLE（没熔断、running），交给下面 RUNNING/持仓 分支重写
        #     注：CAPPED/DEGRADED/SLEEP 已命中时 level != IDLE，不会被下面覆盖。
        if level == ThrottleLevel.IDLE:
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
                        # 2026-08-31 修复 Bug C：根据 position_side 显示多/空方向，不再写死 "LONG_HOLD 持仓中"
                        _side = str(position_side or "").upper()
                        if _side in ("LONG", "BUY"):
                            side_cn = "多单"
                        elif _side in ("SHORT", "SELL"):
                            side_cn = "空单"
                        else:
                            side_cn = "持仓"
                        reason = f"持仓中({side_cn})，本地利润保护足以判定平仓(180s AI 节奏)"
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

        # 把流动性判定摘要塞到前面（NORMAL/HOT等 RUNNING 档），证明按行情不按 UTC 硬睡
        if _liq_prefix and reason:
            reason = _liq_prefix + reason

        base_interval = LEVEL_INTERVALS[level]
        # ---- 3) 行情驱动：动态倍率（CAPPED 档不参与，硬性日成本上限必须严格）----
        dyn_mult = 1.0
        dyn_reason = ""
        if level != ThrottleLevel.CAPPED:
            dyn_mult, dyn_reason = self._dyn_mult(event_pct)
        # 2026-08-31 用户痛点：SLEEP/IDLE 触发 early_wake 时，不能光"这次早叫"就完了，
        #   随后几轮也要主动更紧（否则刚触发 1.8%，下一轮又睡 600s → 1.8% 暴涨阶段完全错过）
        #   → 如果这轮是 early_wake 且档是 SLEEP/IDLE/DEGRADED，再额外 ×0.75（紧一点）
        bonus_reason = ""
        if early_wake and level in (ThrottleLevel.SLEEP, ThrottleLevel.IDLE, ThrottleLevel.DEGRADED):
            bonus = 0.75
            dyn_mult = dyn_mult * bonus
            bonus_reason = f" · 命中早叫档(≥{float(self.cfg['sleep_wake_pct']):g}%)再×0.75奖励"
        interval = max(5, int(round(base_interval * dyn_mult)))  # 保底 5s（防止 bug 卡 0）
        # 合并解释：用户吐槽"固定时间节流错过行情" → 必须一眼看到波动、倍率、最终间隔
        if dyn_reason:
            reason = f"{reason} · {dyn_reason}{bonus_reason} → 动态间隔 {interval}s（基础档 {base_interval}s）"

        # ---- 4) 计算 next_call_ts（如还没设 / 已过期，以 now 为起点）----
        # 冷启动 (last_call_ts==0 且 next_call_ts==0)：本轮立刻可调用，不设等待
        cold_start = self.state.last_call_ts == 0 and self.state.next_call_ts <= 0
        if self.state.next_call_ts <= 0 and not cold_start:
            self.state.next_call_ts = now_ts + interval
        # early_wake / force 允许越过 next_call_ts；cold_start 直接 True 不等冷却
        time_ripe = cold_start or (now_ts >= self.state.next_call_ts)
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
            base_interval_s=base_interval,
            dyn_mult=round(dyn_mult, 4),
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
            # 2026-08-31 行情驱动：给 Dashboard 展示基础档/倍率/动态间隔
            "基础档间隔(秒)": LEVEL_INTERVALS.get(ThrottleLevel(lvl) if lvl in {e.value for e in ThrottleLevel} else ThrottleLevel.NORMAL, 60),
            "动态倍率": round(self._calc_last_dyn_mult(), 4),
            # 2026-08-31 流动性驱动 SLEEP：给 Dashboard 展示价差/量/评分（证明按行情不按 UTC0-6 硬切）
            "最近买卖价差(%)": round(float(self.state.last_spread_pct or 0.0), 4),
            "最近1m成交量(张)": round(float(self.state.last_volume_1m or 0.0), 4),
            "流动性评分": int(self.state.last_liq_score or 0),
            "流动性SLEEP阈值": int(float(self.cfg.get("sleep_liq_score_thr", 60))),
            "流动性结论": (
                "充足→不睡" if lvl != ThrottleLevel.SLEEP.value and int(self.state.last_liq_score or 0) < int(float(self.cfg.get("sleep_liq_score_thr", 60)))
                else "差→进入SLEEP" if lvl == ThrottleLevel.SLEEP.value
                else "一般→正常盯盘"
            ),
        }

    def _calc_last_dyn_mult(self) -> float:
        """根据最近波动算出的动态倍率（Dashboard 展示用，不做 clamp 之外的副作用）。"""
        try:
            mult, _ = self._dyn_mult(self.state.last_event_pct or 0.0)
            return mult
        except Exception:  # noqa: BLE001
            return 1.0
