# -*- coding: utf-8 -*-
"""风控引擎实现（设计文档 · 第十节）。

最高权限模块：
  - 是否允许开仓
  - 仓位大小 / 杠杆
  - 止损计算
  - 熔断控制

规则：
  - 连续亏损 3 次 → 暂停 12 小时
  - 每日亏损 15%   → 停止交易
  - 单笔风险：默认 1% 账户总权益（R = 1%）
  - 默认止损：-1%（无杠杆），对应用 (1% * leverage) 的收益率止损
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from pydantic import BaseModel

from ..core.constants import OrderSide, PositionSide

if TYPE_CHECKING:  # 避免运行时循环 import（config.py 不 import engine）
    from ..broker.base import MarketSpec
    from ..core.config import RiskLimits, TradingConfig


class RiskVerdict(BaseModel):
    """风控判决结果。"""
    allow: bool                          # 是否允许执行
    reason: str = ""                     # 原因（中文）
    suggested_size: float = 0.0          # 允许的下单数量（合约张数 / sz，按交易所 lotSz 已规范化）
    suggested_leverage: int = 1          # 建议杠杆（= min(config.default_leverage, spec.max_lever)）
    stop_loss_price: float = 0.0         # 建议止损价
    # 2026-08-30 新增 USDT 口径字段（给日志 / Dashboard / ycs check 展示）
    suggested_notional_usdt: float = 0.0   # 最终建议下单的名义价值（USDT）= sz × ctVal × entry
    effective_min_notional_usdt: float = 0.0  # 最终生效的最小名义（交易所 minNotional ∨ minSz折算 ∨ config.min_order_notional_usdt）
    effective_max_notional_usdt: float = 0.0  # 最终生效的最大名义（交易所 max × entry ∧ config.max_order_notional_usdt ∧ 保证金×lev）
    sz_by_risk: float = 0.0              # 仅 R 模型推出来的目标张数（未夹约束前）
    sz_by_margin: float = 0.0            # 仅保证金×杠杆推出来的上限张数（未夹约束前）


class RiskEngine:
    """风控引擎（完整实现）。

    2026-08-30：全面改造：
      · 所有阈值（R% / 止损% / 杠杆 / 最小下单 / 名义上下限）都从参数 market_spec / risk_limits / trading_config 读取，
        类常量仅在调用方不传参数时兜底（兼容旧测试）。
      · 计算链统一用『USDT 名义 ↔ 交易所 sz（张数）』双向换算，不再靠硬编码 0.1 张常量判断。
      · RiskVerdict 里新增 USDT 口径字段，让 Dashboard / 日志能直接展示『可下多少 USDT，最小需要多少 USDT，为什么开不了』。
      · 新增 last_verdict / last_verdict_at：把「为什么没开仓」暴露给 /api/status + Dashboard（用户
        之前看到风控状态=允许但实际上没触发开仓，无法分辨是风控拒了还是 AI 信号没到还是信号 pass 但 broker 没下）。
    """

    # ------------------------------------------------------------------
    # 熔断阈值
    # ------------------------------------------------------------------
    MAX_CONSECUTIVE_LOSSES = 3
    COOL_DOWN_HOURS = 12
    MAX_DAILY_LOSS_PCT = 15

    # ------------------------------------------------------------------
    # Dashboard 可观测：最近一次 check_can_open 快照（2026-08-30 新增）
    # ------------------------------------------------------------------
    last_verdict: RiskVerdict | None = None
    last_verdict_at: int = 0          # 秒时间戳
    last_pass_trade_signal_at: int = 0  # 最近一次 AI 信号 + 风控 双过（进入 execute_trade_signal 前记录）

    # 单笔风险：每笔最大亏损 = 账户总权益 × RISK_PER_TRADE_PCT（兜底默认；实际取 risk_limits.risk_per_trade_pct）
    #   14.83U × 5% = 0.7415U/笔
    #   （数学：14.83U × ETH 2466$ × 10X / 每张止损≈0.6165U × sz≥0.1 → 需 R%≥3.33%；留余量取 5%）
    RISK_PER_TRADE_PCT = 5.0

    # 默认止损（相对于入场价的价格百分比，无杠杆）。兜底默认；实际取 risk_limits.stop_loss_price_pct
    #   2.5% 价格止损 × ETH 2466$ ≈ 每张 (0.01) 亏 0.6165U
    DEFAULT_STOP_LOSS_PRICE_PCT = 2.5

    # 单笔建议杠杆（兜底默认；实际取 trading_config.default_leverage，再与 spec.max_lever 取 min）
    DEFAULT_LEVERAGE = 10

    def __init__(self) -> None:
        self.consecutive_losses: int = 0
        self.cooldown_until_ts: int = 0   # 熔断解除 Unix 秒时间戳
        self.daily_start_balance: float = 0.0

    # ------------------------------------------------------------------
    # 状态持久化（与 StateStore["risk"] 对接）
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """导出风控状态，供 StateStore 持久化。"""
        return {
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until_ts": self.cooldown_until_ts,
            "daily_start_balance": self.daily_start_balance,
        }

    def load_dict(self, data: dict[str, Any] | None) -> None:
        """从 StateStore 恢复风控状态。"""
        if not data:
            return
        try:
            self.consecutive_losses = int(data.get("consecutive_losses", 0))
            self.cooldown_until_ts = int(data.get("cooldown_until_ts", 0))
            self.daily_start_balance = float(data.get("daily_start_balance", 0.0))
        except Exception:
            # 字段损坏时不阻塞，等价于冷启动
            self.consecutive_losses = 0
            self.cooldown_until_ts = 0
            self.daily_start_balance = 0.0

    # ------------------------------------------------------------------
    # 日切点 / 平仓结果回调
    # ------------------------------------------------------------------
    def start_new_day(self, current_balance_total: float) -> None:
        """每个交易日开始（或首次启动）调用，固定日初权益。"""
        self.daily_start_balance = float(current_balance_total)

    def on_trade_closed(self, pnl_pct: float) -> None:
        """平仓回调：累计连续亏损、重置。

        Args:
            pnl_pct: 该笔已实现盈亏（百分比，正为盈利，负为亏损）。
        """
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ------------------------------------------------------------------
    # A2. 每日亏损熔断（USDT 绝对值：realized + unrealized 合计 ≤ -limit 立即 HALT）
    #     比百分比更稳，适合 < 50U 超小账户（14.8U 用户现在就是这种情况）
    # ------------------------------------------------------------------
    def check_absolute_daily_loss(
        self,
        *,
        total_now: float,
        realized_pnl_usdt: float,
        unrealized_pnl_usdt: float,
        limit_usdt: float,
    ) -> tuple[bool, str]:
        """A2 绝对日损熔断。

        Args:
            total_now: 当前账户总权益（仅用于日志上下文展示）
            realized_pnl_usdt: 已实现盈亏（当日平仓合计）
            unrealized_pnl_usdt: 未实现浮动盈亏（当前持仓）
            limit_usdt: 阈值（正数，例如 3.0 = 最多允许亏 3 USDT）

        Returns:
            (allow: bool, reason: str) → allow=False 表示已触发 HALT。
        """
        limit = abs(float(limit_usdt or 0))
        realized = float(realized_pnl_usdt or 0)
        unrealized = float(unrealized_pnl_usdt or 0)
        total_loss = realized + unrealized          # 正常为负数
        if limit > 0 and total_loss <= -limit:
            msg = (
                f"[A2 HALT] 当日合计亏损 {total_loss:.4f} U ≤ -{limit:.4f} U 阈值："
                f"已实现 {realized:.4f} U + 未实现 {unrealized:.4f} U；当前权益 {float(total_now or 0):.4f} U。"
                "立即全平 + 停机，待次日手动解除。"
            )
            # 同步熔断：后续 check_can_open 也直接挡
            self.cooldown_until_ts = int(__import__("time").time()) + 86_400  # 直接冻 24 小时
            return False, msg
        return True, (
            f"日损监控：合计 {realized + unrealized:.4f} U / 阈值 -{limit:.4f} U（正常）。"
        )

    # ------------------------------------------------------------------
    # 风控主入口
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 配置兜底（避免运行时 risk_limits 缺失时 AttributeError）
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_risk_pct(rl: Optional["RiskLimits"]) -> tuple[float, float]:
        if rl is not None:
            return float(getattr(rl, "risk_per_trade_pct", None) or 0) or 0.0, \
                float(getattr(rl, "stop_loss_price_pct", None) or 0) or 0.0
        return 0.0, 0.0

    async def check_can_open(
        self,
        *,
        balance_total: float,
        balance_available: float | None = None,
        entry_price: float = 2000.0,  # 默认 ETH 参考价，仅用于仓位估算
        now_ts: int,
        current_pnl_pct: float = 0.0,  # 兼容：旧调用方传入；暂未使用，保留用于未来日中波动熔断
        # 2026-08-30 新增：交易所规则 / 配置（可选，传了就以其为准；不传兜底类常量）
        market_spec: Optional["MarketSpec"] = None,
        risk_limits: Optional["RiskLimits"] = None,
        trading_config: Optional["TradingConfig"] = None,
    ) -> RiskVerdict:
        """开仓前风控：熔断 / 日亏 / 仓位 / 杠杆 / 止损 全量计算（全 USDT 口径 → 再转交易所 sz）。

        新增 Args（2026-08-30，为解决『14.83U 算 0.002 张 < 0.1 张余额不足』问题）：
            market_spec      : 交易所 symbol 的最小下单 / 张面值 / 杠杆上限（可从 Broker.fetch_market_spec() 拿）
            risk_limits      : 实盘硬风控配置（risk_per_trade_pct / stop_loss_price_pct / min/max_order_notional_usdt…）
            trading_config   : 交易配置（default_leverage 等）
        三者都支持 None，缺失时使用类常量兜底（保持对旧测试的向后兼容）。
        """
        # 1) 解析参数（配置优先 → 类常量兜底）
        from ..broker.base import MarketSpec  # 延迟导入，避免 import 时循环
        spec: MarketSpec = market_spec or MarketSpec()

        risk_pct_cfg, sl_pct_cfg = RiskEngine._resolve_risk_pct(risk_limits)
        risk_pct = float(risk_pct_cfg) if risk_pct_cfg > 0 else float(self.RISK_PER_TRADE_PCT)
        sl_pct = float(sl_pct_cfg) if sl_pct_cfg > 0 else float(self.DEFAULT_STOP_LOSS_PRICE_PCT)

        cfg_leverage = int(getattr(trading_config, "default_leverage", None) or 0) if trading_config else 0
        if cfg_leverage <= 0:
            cfg_leverage = int(self.DEFAULT_LEVERAGE)
        leverage = max(1, min(cfg_leverage, int(getattr(spec, "max_lever", cfg_leverage) or cfg_leverage)))

        min_notional_cfg = float(getattr(risk_limits, "min_order_notional_usdt", None) or 0) if risk_limits else 0.0
        max_notional_cfg = float(getattr(risk_limits, "max_order_notional_usdt", None) or 0) if risk_limits else 0.0
        # NOTE: live_max_single_order_usdt 是 stage10 老护栏（core.safety.order_size_sanity_check 消费），
        #   与 RiskEngine 新增的 max_order_notional_usdt **互不叠加**：
        #   - 若用户显式填 max_order_notional_usdt>0 → 用它（语义清晰）；
        #   - 否则就只按交易所/保证金/ R 模型来限制 sz（不要把老 2U 当新名义上限，
        #     否则 14.83U × 2.47U 最小单会被 legacy 2.0U 压成 0.08 张 < minSz 导致全场拒）。

        avail = balance_available if (balance_available is not None and balance_available > 0) else balance_total * 0.9

        def _snap(v: RiskVerdict) -> RiskVerdict:
            """所有拒/通 verdict 都落一份快照 → Dashboard 『为什么不开仓』提示。"""
            self.last_verdict = v
            self.last_verdict_at = int(now_ts)
            return v

        # 1) 熔断期内
        if now_ts < self.cooldown_until_ts:
            remain_h = (self.cooldown_until_ts - now_ts) / 3600
            return _snap(RiskVerdict(
                allow=False,
                reason=f"连续亏损熔断期内，剩余 {remain_h:.1f} 小时，暂停开仓",
                suggested_leverage=leverage,
            ))

        # 2) 连续亏损达到阈值 → 立即熔断，下次 check 起生效
        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self.cooldown_until_ts = now_ts + self.COOL_DOWN_HOURS * 3600
            self.consecutive_losses = 0
            return _snap(RiskVerdict(
                allow=False,
                reason=f"连续亏损 {self.MAX_CONSECUTIVE_LOSSES} 次，启动熔断 {self.COOL_DOWN_HOURS} 小时",
                suggested_leverage=leverage,
            ))

        # 3) 日亏损熔断
        daily_loss_pct = 0.0
        if self.daily_start_balance > 1e-9:
            daily_loss_pct = (1 - balance_total / self.daily_start_balance) * 100
            if daily_loss_pct >= self.MAX_DAILY_LOSS_PCT:
                return _snap(RiskVerdict(
                    allow=False,
                    reason=(
                        f"当日亏损 {daily_loss_pct:.2f}% 超过阈值 {self.MAX_DAILY_LOSS_PCT}%，"
                        "强制停止交易，请次日再启动"
                    ),
                    suggested_leverage=leverage,
                ))

        # 4) 按 R + 交易所规则统一算（先 USDT 口径，再折算 sz 并按 lotSz 夹合法性）
        #    4.1 目标最大亏损(USDT) = balance_total × risk_pct%
        #    4.2 每张绝对止损(USDT) = ct_val × entry × sl_pct%
        #    4.3 → R 模型允许的最大张数 sz_by_risk = max_loss_usdt / (每张绝对止损 × leverage)
        #    4.4 保证金能支持最大张数 sz_by_margin = (avail × leverage) / (entry × ct_val)
        #    4.5 名义上限（交易所+配置双）→ max_sz_by_notional
        #    4.6 raw_sz = min(sz_risk, sz_margin, max_sz_notional)
        #    4.7 按 lotSz floor → clamp → 若 < minSz 或 名义<min_notional → 拒绝并输出 USDT 口径
        per_contract = float(spec.ct_val or 0.01) * max(float(entry_price or 0.0), 1e-9)
        max_loss_usdt = float(balance_total) * (risk_pct / 100.0)
        sl_price_delta = max(float(entry_price) * (sl_pct / 100.0), 0.01)
        per_contract_sl = float(spec.ct_val or 0.01) * sl_price_delta   # 每张止损 USDT（无杠杆）
        stop_loss_price = max(0.01, float(entry_price) - sl_price_delta)  # 默认按多空再调，此处给多头基准

        qty_by_risk = max_loss_usdt / max(per_contract_sl * leverage, 1e-9)
        qty_by_margin = (float(avail) * leverage) / max(per_contract, 1e-9)

        # 名义上限：min( 交易所 max sz × per_contract, 配置层 max_notional_cfg, 安全兜底 )
        exch_max_sz = spec.effective_max_sz(is_market=False)
        exch_max_notional = float(exch_max_sz) * per_contract
        eff_max_notional = exch_max_notional
        if max_notional_cfg > 0 and max_notional_cfg < eff_max_notional:
            eff_max_notional = max_notional_cfg
        max_sz_by_notional = spec.floor_sz(eff_max_notional / max(per_contract, 1e-9))

        raw_sz = min(qty_by_risk, qty_by_margin, max_sz_by_notional)
        # 按交易所步进规范；clamp_sz 返回 0 代表小于 minSz 或越界
        legal_sz = spec.clamp_sz(raw_sz, is_market=False)
        # 再核一次名义：有些品种交易所名义下限比『minSz × entry』更严格
        legal_notional = spec.sz_to_notional(legal_sz, float(entry_price))
        eff_min_notional = spec.effective_min_notional(float(entry_price), min_notional_cfg)
        if legal_sz <= 0 or legal_notional < eff_min_notional:
            # 组装中文拒绝原因（全 USDT 口径，避免再出现『可下张数=0.002 < 最小 0.1』这种让人看不懂的日志）
            raw_notional = raw_sz * per_contract
            # 反推：『摸到最小名义』需要多少本金（=eff_min_notional / leverage）；用户若余额只差一点，能直接看出要补多少
            min_capital_for_min = eff_min_notional / max(1, leverage)
            deny = RiskVerdict(
                allow=False,
                reason=(
                    f"本金太小无法开最小单：当前名义上限可开 ≈ {raw_notional:.2f} USDT "
                    f"（R 模型张数≈{qty_by_risk:.4f} 保证金上限张数≈{qty_by_margin:.4f}）；"
                    f"交易所+配置生效最小名义={eff_min_notional:.2f} USDT "
                    f"（对应 sz≥{spec.min_sz}，lotSz={spec.lot_sz}，spec={spec.source}）；"
                    f"杠杆={leverage}X → 摸到最小单至少需要余额≈{min_capital_for_min:.2f} USDT。"
                    f"可选优化：提高 config.trading.default_leverage（当前 {leverage}X）或 "
                    f"放宽 risk_limits.risk_per_trade_pct（当前 {risk_pct:.1f}%）/ stop_loss_price_pct（当前 {sl_pct:.1f}%）"
                    f"使目标名义 ≥ {eff_min_notional:.2f} USDT。"
                ),
                suggested_leverage=leverage,
                stop_loss_price=stop_loss_price,
                suggested_notional_usdt=raw_notional,
                effective_min_notional_usdt=eff_min_notional,
                effective_max_notional_usdt=eff_max_notional,
                sz_by_risk=qty_by_risk,
                sz_by_margin=qty_by_margin,
            )
            self.last_verdict = deny
            self.last_verdict_at = int(now_ts)
            return _snap(deny)
        # 通过：sz 合法，填充完整字段（含 USDT 口径，供运行日志展示）
        suggested_notional = legal_notional
        verdict = RiskVerdict(
            allow=True,
            reason=(
                f"风控通过：连亏={self.consecutive_losses} 日亏={daily_loss_pct:.2f}% "
                f"单R={risk_pct:.2f}% 止损=±{sl_pct:.2f}% 杠杆={leverage}X "
                f"名义={suggested_notional:.2f}U sz={legal_sz:{'' if spec.sz_decimals<=0 else '.'+str(spec.sz_decimals)+'f'}} "
                f"min_notional={eff_min_notional:.2f}U max_notional={eff_max_notional:.2f}U"
            ),
            suggested_size=float(legal_sz),
            suggested_leverage=int(leverage),
            stop_loss_price=float(stop_loss_price),
            suggested_notional_usdt=float(suggested_notional),
            effective_min_notional_usdt=float(eff_min_notional),
            effective_max_notional_usdt=float(eff_max_notional),
            sz_by_risk=float(qty_by_risk),
            sz_by_margin=float(qty_by_margin),
        )
        # 2026-08-30：落盘一份最新 verdict 给 Dashboard「为什么不开仓」卡片
        return _snap(verdict)

    # ------------------------------------------------------------------
    # 辅助：按方向返回正确止损价（check_can_open 返回的是多头基准）
    # ------------------------------------------------------------------
    @staticmethod
    def orient_stop_loss(entry_price: float, sl_price_delta: float, side: OrderSide | PositionSide) -> float:
        """对多头：止损低于入场价；对空头：止损高于入场价。"""
        if side in (OrderSide.BUY, PositionSide.LONG):
            return max(0.01, entry_price - sl_price_delta)
        return entry_price + sl_price_delta
