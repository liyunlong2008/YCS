"""
启动前安全自检：
  - 实盘(live=true)：OKX / AI 密钥为空或占位 → raise RuntimeError，阻止启动
  - 纸盘(live=false)：占位值仅警告，不阻止启动
占位值定义：
  1) None / 空串 / 仅空白
  2) 以 "YOUR_" 开头的示例占位（不区分大小写）
  3) 常见占位："xxx", "TODO", "changeme", "example", "test", "placeholder"（小写比较）
"""
from __future__ import annotations

import re
from loguru import logger


# 占位特征：命中任一即视为占位
_PLACEHOLDER_PREFIXES = ("your_",)
_PLACEHOLDER_EXACT = {
    "xxx", "todo", "changeme", "change_me", "example",
    "test", "placeholder", "demo", "none", "null", "",
}
_PLACEHOLDER_RE = re.compile(r"^\s*(your[-_]|changeme|todo[\s_-]*$|xxxx+$)", re.IGNORECASE)


def _is_placeholder(value: object) -> bool:
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    low = s.lower()
    if low in _PLACEHOLDER_EXACT:
        return True
    for prefix in _PLACEHOLDER_PREFIXES:
        if low.startswith(prefix):
            return True
    if _PLACEHOLDER_RE.match(s):
        return True
    # 连续 5 个以上的 X / *（常作遮罩占位）
    if re.fullmatch(r"[xX\*]{5,}", s):
        return True
    return False


def validate_runtime_credentials(
    *,
    live: bool,
    okx_api_key: str,
    okx_secret: str,
    okx_passphrase: str,
    ai_api_key: str,
) -> bool:
    """
    Returns True 表示校验通过；live=True 不通过时 raise RuntimeError。
    纸盘模式仅打印 WARNING 级日志。
    """
    problems: list[str] = []

    checks = [
        ("okx.api_key",       okx_api_key),
        ("okx.secret",        okx_secret),
        ("okx.passphrase",    okx_passphrase),
        ("ai.api_key",        ai_api_key),
    ]

    # 纸盘模式下 OKX 占位允许（因为用 PaperBroker），但 AI 占位仍提示
    paper_only_warnings: list[str] = []

    for name, value in checks:
        if _is_placeholder(value):
            if not live and name.startswith("okx."):
                paper_only_warnings.append(f"{name}=占位值（纸盘模式跳过）")
                continue
            problems.append(name)

    if not live:
        if paper_only_warnings:
            for item in paper_only_warnings:
                logger.warning("[安全自检·纸盘] {}", item)
        if _is_placeholder(ai_api_key):
            logger.warning("[安全自检·纸盘] ai.api_key=占位值：AI 将使用回退默认行情(震荡/中性)")
        return True

    # 实盘：任一占位即拒绝启动
    if problems:
        raise RuntimeError(
            "实盘模式启动被安全自检拦截：以下密钥仍是占位值，请填入真实凭证后再启动。\n"
            "  · 问题字段: " + ", ".join(problems) + "\n"
            "  · 纸盘/仿真模式请修改 config.yaml → trading.live: false\n"
            "  · 占位特征: 空值 / YOUR_开头 / xxx / TODO / changeme / placeholder 等"
        )
    return True


# ============================================================================
# 实盘护栏函数（按 2026-08-29 用户方案：小仓位直接上实盘前必须补齐的 7 条硬风控）
# ============================================================================
from pathlib import Path  # noqa: E402
import secrets            # noqa: E402
import time as _time      # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402
if TYPE_CHECKING:
    from ..broker.base import Position  # pragma: no cover


# ---------------------------------------------------------------------------
# A3. 订单大小双因子 sanity check（下单前必须调，挡住 AI / Controller 逻辑 bug）
# ---------------------------------------------------------------------------
def order_size_sanity_check(
    *,
    qty_contracts: float,
    last_price: float,
    total_equity: float,
    max_single_usdt: float,
    position_change_pct: float,
) -> tuple[bool, str]:
    """订单大小 sanity 双因子。

    Returns:
      (reject: bool, reason: str) → True 表示拒绝这笔下单。
    """
    qty = abs(float(qty_contracts or 0))
    price = max(float(last_price or 0), 1e-12)
    equity = max(float(total_equity or 0), 0.0)
    max_single = max(float(max_single_usdt or 0), 0.0)
    change_pct = float(position_change_pct or 0)  # 0.10 = 10%

    nominal = qty * price  # 订单名义价值 USDT
    if max_single > 0 and nominal > max_single:
        return True, (
            f"单笔订单名义价值 {nominal:.4f} U 超过上限 {max_single:.4f} U（A3 护栏）"
        )
    if change_pct > 0 and equity > 0:
        max_change_usdt = equity * change_pct
        if nominal > max_change_usdt:
            return True, (
                f"仓位变动率 {nominal:.4f} U / 总资产 {equity:.4f} U = "
                f"{nominal / equity * 100:.2f}% 超过上限 {change_pct * 100:.2f}%（A3 护栏）"
            )
    return False, "sanity 通过"


# ---------------------------------------------------------------------------
# A4. 真实仓位对账（每 60s 跑一次，不一致立刻 halt + 全平）
# ---------------------------------------------------------------------------
def reconcile_position(
    local: "Position",
    exchange: "Position",
    *,
    tolerance_usdt: float = 0.5,
    reference_price: float | None = None,
) -> tuple[bool, str]:
    """Position Reconciliation。

    Returns:
      (halt: bool, reason: str) → True 表示触发对账失败：必须立即 halt 所有开新仓 + 紧急对齐仓位。
    """
    price = max(float(reference_price or 0), 1e-12)
    size_diff = abs(float(local.size or 0) - float(exchange.size or 0))
    usdt_diff = size_diff * price
    side_diff = local.side != exchange.side
    # 方向不同 → 必 halt；数量差 > tolerance_usdt → 必 halt
    if side_diff or usdt_diff > tolerance_usdt:
        return True, (
            f"仓位对账失败（A4 护栏）：本地 {local.side.value} × {local.size:.6f} vs "
            f"交易所 {exchange.side.value} × {exchange.size:.6f}，差 {usdt_diff:.3f} U（容差 {tolerance_usdt} U）"
        )
    return False, "对账一致"


# ---------------------------------------------------------------------------
# A5. Kill-Switch 兜底文件通道（/api/kill 和 ycsctl kill 挂了时，touch data/EMERGENCY_HALT 救命）
# ---------------------------------------------------------------------------
def check_emergency_halt_file(file_path: str | Path) -> tuple[bool, str]:
    """检查 EMERGENCY_HALT 空文件是否存在（主循环每秒轮询一次）。"""
    p = Path(file_path)
    if p.is_file():
        return True, f"A5-兜底：检测到 EMERGENCY_HALT 文件（{p}），立即 halt"
    return False, ""


# ---------------------------------------------------------------------------
# A6. 幂等键生成：ycs_<13 位毫秒>_<8 位 hex 随机>，保证毫秒级并发不重复、格式可追溯
# ---------------------------------------------------------------------------
def generate_client_order_id() -> str:
    """下单必带 clientOrderId，实现幂等 & 超时重查 & journal 关联。"""
    ts_ms = int(_time.time() * 1000)
    rand8 = secrets.token_hex(4)  # 4 bytes = 8 hex chars
    return f"ycs_{ts_ms}_{rand8}"


# ---------------------------------------------------------------------------
# A7. Shadow 影子模式总闸门：shadow_mode=True → 100% 拦截任何实盘真发单
# ---------------------------------------------------------------------------
def should_block_real_orders(*, shadow_mode: bool) -> bool:
    """A7 影子模式总闸门：所有 Broker.place_order 真正调用交易所私有 API 前必须调用一次。"""
    return bool(shadow_mode)


# ---------------------------------------------------------------------------
# 自动风险检测（供 /api/diag 中 risks 段使用）
# ---------------------------------------------------------------------------
def detect_risks(cfg: "AppConfig | None") -> list[str]:
    """返回 Top 3~5 条自动检测的严重警告（AI 分析项目缺陷的输入）。"""
    risks: list[str] = []
    if cfg is None:
        risks.append("尚未加载 AppConfig（可能单测模式 / Controller 未注入）")
        return risks

    # Key 占位检测
    keys = [
        ("okx.api_key", cfg.okx.api_key),
        ("okx.secret", cfg.okx.secret),
        ("okx.passphrase", cfg.okx.passphrase),
        ("ai.api_key", cfg.ai.api_key),
    ]
    placeholder_keys = [name for name, v in keys if _is_placeholder(v)]
    if placeholder_keys:
        risks.append(f"[FATAL] 仍有 {len(placeholder_keys)} 个凭证是占位值：{', '.join(placeholder_keys)}。实盘 live=true 会被安全自检拦截启动。")

    # kill_switch_token 仍是默认占位
    if cfg.risk_limits.kill_switch_token.startswith("YCS_KILL_CHANGEME_"):
        risks.append("[WARN] risk_limits.kill_switch_token 还是默认示例值，/api/kill 不安全（生产必须换 32 位随机串）。")

    # 实盘但 shadow_mode=false → 提醒先跑 shadow 观察
    if cfg.trading.live and not cfg.risk_limits.shadow_mode:
        risks.append("[INFO] 已进入实盘(live=true)且未启用影子模式：建议先 shadow_mode=true 观察 ≥6 小时，确认链路/限频/下单大小正确后再真发。")

    # 本金硬锁 < 实际已知余额阈值提示
    if cfg.risk_limits.live_max_equity_usdt < 1.0:
        risks.append("[WARN] live_max_equity_usdt < 1.0 U，会导致所有开仓被 sanity 拒绝。")

    # 日损熔断阈值过大/过小
    if cfg.risk_limits.live_max_daily_loss_usdt > cfg.risk_limits.live_max_equity_usdt:
        risks.append(
            f"[WARN] 日损熔断 {cfg.risk_limits.live_max_daily_loss_usdt} U > 本金上限 "
            f"{cfg.risk_limits.live_max_equity_usdt} U，熔断形同虚设。"
        )
    return risks[:5]
