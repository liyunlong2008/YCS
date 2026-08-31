# -*- coding: utf-8 -*-
"""FastAPI Dashboard（中文界面，设计文档 · 第二十节）。

提供：
  GET /                       仪表盘首页（中文 HTML）
  GET /api/health             健康检查
  GET /api/status             系统总览（中文键）
  GET /api/balance            账户余额
  GET /api/position           当前持仓
  GET /api/trades?limit=N     最近交易流水
  GET /api/ai/analyze         调 AI 分析并返回中文结果（无行情生产者时返回占位）

运行时：
  run.py 会把 TradingController 注入 `app.state.runtime["controller"]`；
  若未注入（例如单测、开发只启 api），则返回默认空结构，不阻塞启动。
"""

from __future__ import annotations

import json as _json
import os as _os
import re as _re
import shutil as _shutil
import subprocess as _sp
import sys as _sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse as _JSONResponse
from fastapi.testclient import TestClient  # noqa: F401  —— 方便测试里直接 import


# =============================================================================
# 全局 JSON 编码约定：显式 charset=utf-8 + ensure_ascii=False
#   · 避免 Windows PowerShell/cmd 下 curl 管道默认 GBK 把 UTF-8 中文解成乱码
#   · 内部拼接字符串时，清掉 unpaired surrogate（Windows 端常见 GBK→UTF-8 污染）
# =============================================================================
_SURROGATE_RE = _re.compile("[\uD800-\uDFFF]")


class UTF8JSONResponse(_JSONResponse):
    """FastAPI JSONResponse 子类：显式 media_type 带 charset=utf-8；且 ensure_ascii=False，
       这样 Windows 终端 curl | python -m json.tool 不用加 --utf8 也能正确显示中文。"""
    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        # 遍历字符串子节点，清掉孤立 surrogate（上游链路脏输入时兜底）
        def _clean(obj: Any) -> Any:
            if isinstance(obj, str):
                return _SURROGATE_RE.sub("\ufffd", obj)
            if isinstance(obj, dict):
                return {_clean(k): _clean(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_clean(x) for x in obj]
            return obj
        cleaned = _clean(content)
        return _json.dumps(
            cleaned, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")


# FastAPI 默认 response_class → UTF8JSONResponse
_DEFAULT_RESPONSE_CLASS = UTF8JSONResponse

# 项目根：统一用 app/api/app.py → parents[2]（app/ → 项目根 /workspace）
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def _diag_run_pytest(
    pytest_args: list[str],
    *,
    project_root: Path,
    timeout_seconds: int = 90,
) -> tuple[bool, str]:
    """stage8 / stage9 快速自检。优先用 `uv run pytest`；若找不到 uv（Windows 常
    见：未把 uv 加入 PATH 或用全局 python 直接启 run.py），回退到 `sys.executable -m pytest`。

    返回 (passed, tail_summary)：passed=(returncode==0)，tail_summary 取 stderr+stdout 末
    3 行（pytest summary 形如「18 passed」「5 failed」）。
    """
    # 优先 uv（复用项目锁版本更稳）
    use_uv = _shutil.which("uv") is not None
    if use_uv:
        argv: list[str] = ["uv", "run", "pytest", *pytest_args]
    else:
        py = _sys.executable or "python"
        argv = [py, "-m", "pytest", *pytest_args]
    env = _os.environ.copy()
    # 清空代理变量（fixtures 都是本地离线，不需要代理；防止 Windows 用户系统代理污染 pytest 网络探测）
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    common = dict(
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    try:
        r = _sp.run(argv, **common)  # type: ignore[arg-type]
    except FileNotFoundError:
        if use_uv:
            return False, "未找到 uv 可执行文件"
        return False, f"未找到 python pytest：{argv[0]}"
    except _sp.TimeoutExpired:
        return False, f"pytest 超时（>{timeout_seconds}s，参数={pytest_args}）"
    except Exception as e:  # noqa: BLE001
        return False, f"执行异常 {type(e).__name__}: {e}"

    ok = (r.returncode == 0)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    tail = "\n".join(lines[-3:])
    if not tail:
        tail = f"exit={r.returncode}"
    return ok, tail

# 中文映射表（设计文档 · 第二十节）
ZH_STATUS = {
    "RUNNING": "运行中",
    "STOPPED": "停止",
    "RECOVERING": "恢复中",
    "ERROR": "异常",
    "PAPER": "纸盘模式",
    "LIVE": "实盘模式",
    "LONG": "做多",
    "SHORT": "做空",
    "FILLED": "已成交",
    "PARTIAL": "部分成交",
    "CANCELED": "已撤销",
}


def _zh(value: str | None) -> str:
    if not value:
        return "—"
    return ZH_STATUS.get(value, value)


def _infer_direction_from_regime(regime_cn: str) -> str:
    """从 AI 市场状态中文推断建议方向（兜底；有 suggested_direction 时走它）。"""
    r = str(regime_cn or "")
    if "上涨" in r:
        return "做多 LONG"
    if "下跌" in r:
        return "做空 SHORT"
    if "震荡" in r or "低波动" in r:
        return "观望 WAIT"
    if "波动" in r:
        return "谨慎观望"
    return "—"


def create_app(
    config_path: Path | str | None = None,
    *,
    runtime: dict[str, Any] | None = None,
    on_startup: list[Callable[[], Any]] | None = None,
    on_shutdown: list[Callable[[], Any]] | None = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    Args:
        config_path: 配置文件路径。None 时按 app.core.config.default_config_path 规则解析：
            1) $CONFIG_PATH env > 2) <项目根>/config.yaml。
        runtime: 可选预填充 runtime 字典（单测 / 离线调用使用）
        on_startup: 可选启动期同步回调列表（lifespan 启动时调用）
        on_shutdown: 可选关闭期同步回调列表（lifespan 关闭期调用）
    """

    # ---- 配置加载：优先显式参数 > $CONFIG_PATH env > 项目根默认 ----
    cfg: Any | None = None
    try:
        from ..core.config import load_config  # noqa: PLC0415
        cfg = load_config(config_path)
    except FileNotFoundError:
        cfg = None
    except Exception as exc:  # noqa: BLE001
        from loguru import logger  # noqa: PLC0415
        logger.warning("create_app 加载配置失败：{}（降级为无 config 骨架模式）", exc)
        cfg = None

    @asynccontextmanager
    async def _lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        # 启动阶段
        for fn in on_startup or []:
            try:
                result = fn()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                from loguru import logger
                logger.exception("on_startup 回调异常")
        yield
        # 关闭阶段
        for fn in on_shutdown or []:
            try:
                result = fn()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                from loguru import logger
                logger.exception("on_shutdown 回调异常")

    app = FastAPI(
        title="云龙挑战赛 Dashboard",
        version="1.0.0",
        description="云龙挑战赛（YCS）：ETH-USDT-SWAP 单品种 AI 分析 + Maker 优先 + 严格风控 + 自动恢复 + 利润保护 自动交易系统。",
        docs_url="/docs",
        lifespan=_lifespan,
        default_response_class=UTF8JSONResponse,
    )

    # ------------------------------------------------------------------
    # 全局 runtime：run.py / 测试用例 会注入 controller
    # ------------------------------------------------------------------
    app.state.runtime: dict[str, Any] = {
        "config": cfg,        # create_app 时加载好的 AppConfig（Dashboard 骨架用）
        "broker": None,
        "ai": None,
        "risk": None,
        "storage": None,
        "controller": None,
        "state_store": None,
    }
    if runtime:
        app.state.runtime.update(runtime)

    def _get_controller(request: Request):
        ctl = request.app.state.runtime.get("controller")
        if ctl is None:
            raise HTTPException(status_code=503, detail="控制器尚未初始化，请先在 run.py 中注入 TradingController")
        return ctl

    def _build_risk_tip(
        *,
        last_risk: Any,
        pass_ts: int,
        allow_val: Any,
        ai_regime: Any,
        ai_conf: int,
        has_pos: bool = False,
        pos_side_cn: str = "空仓",
        pos_size: float = 0.0,
        pos_entry: float = 0.0,
        pos_mark: float = 0.0,
        pos_leverage: int = 1,
        pos_upl: float = 0.0,
    ) -> str:
        """把风控 + AI 信号 + 缺口本金 压缩成 Dashboard 一行人类可读提示。

        2026-08-31 新增 has_pos 分支：若当前真有持仓（ShadowBroker 虚拟持仓也算），
        直接显示「持仓概况」，避免用户看到 stale 的「信号双过 Ns 前 等成交」误报。
        """
        import time as _tip_t  # noqa: PLC0415
        now = int(_tip_t.time())
        if bool(has_pos) and float(pos_size or 0.0) > 0:
            upl_sgn = f"+{float(pos_upl or 0.0):+.4f}U"
            return (
                f"📦 已持仓 {float(pos_size or 0.0):.1f} 张({pos_side_cn})｜"
                f"成本 {float(pos_entry or 0.0):.1f}$ / 现价 {float(pos_mark or 0.0):.1f}$"
                f"{(' / '+str(int(pos_leverage or 1))+'X') if int(pos_leverage or 1) > 1 else ''}"
                f"｜浮盈亏 {upl_sgn}｜后续：AI 风控触发利润保护平仓"
            )
        if not isinstance(last_risk, dict):
            return "启动中：等待第一轮风控评估（10s 内）"
        conclusion = str(last_risk.get("结论") or "未执行")
        reason = str(last_risk.get("原因") or "")[:80]
        ai_ok = (isinstance(ai_regime, str)
                 and ai_regime in ("上涨趋势", "下跌趋势")
                 and int(ai_conf or 0) >= 50)
        gap = last_risk.get("缺口本金(USDT)")
        sug_lev = last_risk.get("建议杠杆(X)")
        sug_nom = last_risk.get("建议名义价值(USDT)")
        min_nom = last_risk.get("最小名义(USDT)")
        ai_txt = (
            f"AI信号到位[{ai_regime} conf={ai_conf}]"
            if ai_ok
            else f"AI信号未到[{ai_regime or '暂无'} conf={ai_conf}；需TREND_UP/DOWN+≥50才开]"
        )
        if int(pass_ts or 0) > 0:
            age = now - int(pass_ts or 0)
            hh, rem = divmod(age, 3600); mm, ss = divmod(rem, 60)
            age_txt = (f"{hh}h{mm:02d}m{ss:02d}s前" if hh > 0
                       else (f"{mm}m{ss:02d}s前" if mm > 0 else f"{ss}s前"))
            if conclusion == "通过":
                return f"✅ 信号双过{age_txt}，等成交/下一轮再评估 · 风控结论=通过(名义{sug_nom or '?'}U ≥ min {min_nom or '?'}U @ {sug_lev or '?'}X) · {ai_txt}"
        if conclusion == "拒绝":
            tail = ""
            if isinstance(gap, (int, float)) and float(gap) > 0:
                tail = (f" · ⚠️还差 ≈ {float(gap):.2f}U 本金才摸到最小单；"
                        f"补救：提高杠杆(当前 {sug_lev if sug_lev else '?'}X)"
                        f"或调大 R%/止损%")
            return f"❌ 风控拒绝 · {reason}{tail}"
        if conclusion == "通过":
            if not ai_ok:
                return f"🟡 风控通过(名义 {sug_nom or '?'}U ≥ min {min_nom or '?'}U @ {sug_lev or '?'}X) · 等{ai_txt}"
            return f"🟢 风控通过 + {ai_txt} · 信号应已发送，查『最近交易』或 journalctl -u ycs | grep [主循环]"
        # 未执行
        return "启动中：等待第一轮风控评估（10s 内）"

    async def _collect_dashboard_data(rt: dict[str, Any]) -> dict[str, Any]:
        """服务端聚合首页需要的字段；优先 Controller（实时）→ state_store（已持久化）→ 默认骨架。
           · 用 ctl.get_status_dict() 的中文结构对齐 /api/status，首屏 AI 就不再空。
           · 额外补 journal（最近交易）。
        """
        # 1) 运行模式
        mode_cn = "纸盘模式"
        cfg = rt.get("config")
        if cfg is not None and hasattr(cfg, "trading"):
            try:
                from ..core.constants import RunMode
                mode_cn = "实盘模式" if getattr(cfg.trading, "live", False) else "纸盘模式"
            except Exception:
                mode_cn = "纸盘模式"
        if cfg is not None and hasattr(cfg, "risk_limits"):
            if bool(getattr(cfg.risk_limits, "shadow_mode", False)):
                mode_cn = f"{mode_cn}(影子 SHADOW)"

        # 2) Controller 实时数据（首屏优先，没启动则 fallback state_store）
        ctl = rt.get("controller")
        status_from_ctl: dict[str, Any] | None = None
        if ctl is not None and hasattr(ctl, "get_status_dict"):
            try:
                status_from_ctl = ctl.get_status_dict() or {}
                if isinstance(status_from_ctl, dict) and status_from_ctl.get("运行模式"):
                    mode_cn = status_from_ctl["运行模式"]
            except Exception:
                status_from_ctl = None

        # 3) state_store：Controller 没有时的骨架
        store = rt.get("state_store")
        snapshot: dict[str, Any] = {}
        if store is not None:
            try:
                snapshot = store.load() or {}
            except Exception:
                snapshot = {}
        bal = snapshot.get("balance") or {}
        stats = snapshot.get("stats") or {}
        risk_dict = snapshot.get("risk") or {}
        pm_dict = snapshot.get("position_manager") or {}

        # ---- 基本数值：Controller 优先 ----
        total = float(
            (status_from_ctl.get("账户余额总权益") if isinstance(status_from_ctl, dict) else None)
            or bal.get("total", 0.0) or 0.0
        )
        available = float(
            (status_from_ctl.get("可用保证金") if isinstance(status_from_ctl, dict) else None)
            or bal.get("available", total) or total
        )
        upl = float(
            (status_from_ctl.get("未实现盈亏") if isinstance(status_from_ctl, dict) else None)
            or bal.get("unrealized_pnl", 0.0) or 0.0
        )
        daily_start = float(risk_dict.get("daily_start_balance", total or 1000.0) or 1000.0)
        total_pnl_pct = float(
            (status_from_ctl.get("累计收益率(%)") if isinstance(status_from_ctl, dict) else None)
            or stats.get("total_pnl_pct", 0.0) or 0.0
        )
        wins = int(
            (status_from_ctl.get("盈利次数") if isinstance(status_from_ctl, dict) else None)
            or stats.get("wins", 0) or 0
        )
        losses = int(
            (status_from_ctl.get("亏损次数") if isinstance(status_from_ctl, dict) else None)
            or stats.get("losses", 0) or 0
        )
        closed = wins + losses
        wr = (wins / closed * 100) if closed > 0 else 0.0
        sys_status_cn = (status_from_ctl or {}).get("系统状态") or snapshot.get("status") or "运行中"

        # 启动时间 + 运行时长：Controller 优先；state_store started_at（兼容 int/字符串）兜底 —— 修复 started_at=None 空值
        import datetime as _dt_up, time as _tt_up  # noqa: PLC0415
        start_epoch: int | None = (status_from_ctl or {}).get("启动时间戳(epoch秒)") if isinstance(status_from_ctl, dict) else None
        start_local: str | None = (status_from_ctl or {}).get("启动时间") if isinstance(status_from_ctl, dict) else None
        upt_s: int | None = (status_from_ctl or {}).get("运行时长(秒)") if isinstance(status_from_ctl, dict) else None
        upt_human: str | None = (status_from_ctl or {}).get("运行时长") if isinstance(status_from_ctl, dict) else None
        if start_epoch is None:
            raw_sa = snapshot.get("started_at")
            if isinstance(raw_sa, int) and raw_sa > 0:
                start_epoch = raw_sa
            elif isinstance(raw_sa, str):
                try:
                    start_epoch = int(_dt_up.datetime.strptime(raw_sa, "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:  # noqa: BLE001
                    start_epoch = None
        if start_epoch:
            if not start_local:
                try:
                    start_local = _tt_up.strftime("%Y-%m-%d %H:%M:%S", _tt_up.localtime(int(start_epoch)))
                except Exception:  # noqa: BLE001
                    start_local = None
            if upt_s is None:
                s_ = max(int(_tt_up.time()) - int(start_epoch), 0)  # max(0): 时钟漂移不显示负运行时间
                upt_s = s_
                h, rem = divmod(s_, 3600); m, s2 = divmod(rem, 60)
                upt_human = (f"{h}h{m:02d}m{s2:02d}s" if h > 0 else (f"{m}m{s2:02d}s" if m > 0 else f"{s2}s"))

        # 4) 风控：Controller 实时 block 优先
        rsk_ctl = (status_from_ctl or {}).get("风控状态") or {}
        consec = int(rsk_ctl.get("连续亏损次数") or risk_dict.get("consecutive_losses", 0) or 0)
        allow_val = rsk_ctl.get("是否允许开仓")
        if allow_val is None:
            allow_bool = bool(risk_dict.get("allow_trading", True))
            allow_val = "是" if allow_bool else "否"
        cooldown_ts = rsk_ctl.get("熔断冷却至(秒时间戳)") or risk_dict.get("cooldown_until") or 0
        if allow_val == "否" and cooldown_ts:
            try:
                import time as _t
                cd_left_s = int(cooldown_ts) - int(_t.time())
                if cd_left_s > 0:
                    h, m = divmod(cd_left_s, 3600); m, s = divmod(m, 60)
                    allow_val = f"否（冷却剩 {h}h{m:02d}m{s:02d}s）"
            except Exception:
                allow_val = f"否（冷却至 ts={cooldown_ts}）"
        daily_loss_pct = risk_dict.get("daily_loss_pct") or 0.0
        daily_status = "正常" if daily_loss_pct > -15.0 else f"已触发日亏限制（{daily_loss_pct:.2f}%）"

        # 5) 持仓：Controller.broker 实时 position 优先（ShadowBroker 虚拟持仓必须在这里显现！）
        #    state_store.position 作为上次 bg_main_loop 持久化快照兜底
        pos_side = "空仓"
        pos_size = 0.0; pos_entry = 0.0; pos_mark = 0.0; pos_upl = 0.0; pos_leverage = 1
        realtime_pos_ok = False
        if ctl is not None and hasattr(ctl, "broker"):
            try:
                import asyncio as _aio_pos
                sym_pos = (getattr(getattr(cfg, "trading", None), "symbol", None) if cfg else None) or "ETH-USDT-SWAP"
                try:
                    loop = _aio_pos.get_running_loop()  # FastAPI 在 async def 里；/ 端点走 GET
                except RuntimeError:
                    loop = None
                if loop is None:
                    # 理论上 index 端点是 async 一定有 loop；这里以防万一不阻塞
                    p_realtime = None
                else:
                    p_realtime = await ctl.broker.get_position(sym_pos)
                if p_realtime is not None:
                    rside = _zh(getattr(p_realtime, "side", PositionSide.FLAT).value
                                if hasattr(getattr(p_realtime, "side", None), "value")
                                else str(getattr(p_realtime, "side", "FLAT")))
                    rside = "空仓" if rside in ("—", "FLAT") else rside
                    rsz = float(getattr(p_realtime, "size", 0.0) or 0.0)
                    if rsz > 0:
                        realtime_pos_ok = True
                        pos_side = rside
                        pos_size = rsz
                        pos_entry = float(getattr(p_realtime, "entry_price", 0.0) or 0.0)
                        pos_mark = float(getattr(p_realtime, "mark_price", 0.0) or 0.0)
                        pos_upl = float(getattr(p_realtime, "unrealized_pnl", 0.0) or 0.0)
                        pos_leverage = int(getattr(p_realtime, "leverage", 1) or 1)
            except Exception:  # noqa: BLE001
                realtime_pos_ok = False
        if not realtime_pos_ok:
            pos_saved = snapshot.get("position") or {}
            if pos_saved:
                pos_side = _zh(pos_saved.get("side") or "FLAT")
                pos_side = "空仓" if pos_side in ("—", "FLAT") else pos_side
                pos_size = float(pos_saved.get("size", 0.0) or 0.0)
                pos_entry = float(pos_saved.get("entry_price", 0.0) or 0.0)
                pos_mark = float(pos_saved.get("mark_price", 0.0) or 0.0)
                pos_upl = float(pos_saved.get("unrealized_pnl", 0.0) or 0.0)
                pos_leverage = int(pos_saved.get("leverage", 1) or 1)
        # 若 state_store 位置空但 upl!=0，标记价格从 snapshot.mark_price 填
        if pos_mark <= 0:
            pos_mark = float(snapshot.get("mark_price", 0.0) or 0.0)
        # 利润保护
        current_lock = float(pm_dict.get("current_lock_pct", 0.0) or 0.0)
        trailing = float(pm_dict.get("trailing_stop_price", 0.0) or 0.0)
        protect_txt = "未启用"
        if current_lock > 0:
            protect_txt = f"已锁 {current_lock:.1f}% 利润"
        elif trailing > 0:
            protect_txt = f"移动止损价 {trailing:.2f}"

        # 6) AI 判断：**必须取 Controller._last_ai_block() 实时结果**（否则首屏一直空）
        ai_block: dict[str, Any] = {}
        if isinstance(status_from_ctl, dict) and isinstance(status_from_ctl.get("最近AI判断"), dict):
            ai_block = dict(status_from_ctl["最近AI判断"])
        # state_store 兜底（Controller 没注入时）
        if not ai_block:
            ai_saved = snapshot.get("last_ai") or {}
            regime_map_local = {"TREND_UP": "上涨趋势", "TREND_DOWN": "下跌趋势", "RANGE": "震荡", "VOLATILE": "波动", "LOW_VOLATILITY": "低波动"}
            reg_val = ai_saved.get("market_regime") or "—"
            ai_regime_cn_local = regime_map_local.get(str(reg_val).upper(), str(reg_val))
            ai_block = {
                "市场状态": ai_regime_cn_local,
                "置信度": ai_saved.get("confidence") or 0,
                "理由": ai_saved.get("reason_short") or ai_saved.get("reason") or "暂无",
                "时间": ai_saved.get("ts") or ai_saved.get("time") or None,
            }
        # 补：建议方向（给 Dashboard『建议方向』字段，AI 分析里常含 suggest_direction）
        ai_saved_any = snapshot.get("last_ai") or {}
        direction = str(
            (status_from_ctl or {}).get("建议方向")
            or (ai_block or {}).get("建议方向")
            or ai_saved_any.get("suggested_direction")
            or _infer_direction_from_regime(str((ai_block or {}).get("市场状态") or "—"))
        )
        reason_short = str((ai_block or {}).get("理由") or "暂无")
        if len(reason_short) > 120:
            reason_short = reason_short[:120] + "…"

        # 6.5) AI 节流状态（2026-08-30 新增）
        throttle_ctl = {}
        if isinstance(status_from_ctl, dict) and isinstance(status_from_ctl.get("AI节流状态"), dict):
            throttle_ctl = dict(status_from_ctl["AI节流状态"])
        thr_level = str(throttle_ctl.get("节流级别") or "NORMAL")
        thr_color = str(throttle_ctl.get("节流颜色") or "#e6f4ea;color:#137333")
        thr_countdown = int(throttle_ctl.get("倒计时(秒)") or 0)
        thr_daily_calls = int(throttle_ctl.get("当日调用次数") or 0)
        thr_early_wakes = int(throttle_ctl.get("当日早叫次数") or 0)
        thr_failures = int(throttle_ctl.get("连续失败次数") or 0)
        thr_volatility = float(throttle_ctl.get("最近波动(%)") or 0)
        thr_reason = str(throttle_ctl.get("级别原因") or "初始化")
        if len(thr_reason) > 80:
            thr_reason = thr_reason[:80] + "…"

        # 6.6) 时间同步漂移状态（2026-08-30：Dashboard 顶部 tag 显示）
        time_sync_ctl = {}
        if isinstance(status_from_ctl, dict) and isinstance(status_from_ctl.get("时间同步状态"), dict):
            time_sync_ctl = dict(status_from_ctl["时间同步状态"])
        if not time_sync_ctl:
            ts_raw = snapshot.get("time_sync") or {}
            drift_ms = int(ts_raw.get("drift_ms") or 0)
            drifted_pause = bool(ts_raw.get("drifted_pause"))
            last_sync_at = int(ts_raw.get("last_sync_at") or 0)
            import time as _tt
            age_s = int(_tt.time()) - last_sync_at if last_sync_at > 0 else None
            age_txt = "未同步"
            if age_s is not None and age_s < 60:
                age_txt = f"{age_s}s 前同步"
            elif age_s is not None and age_s < 3600:
                age_txt = f"{age_s//60}m{age_s%60:02d}s 前同步"
            elif age_s is not None:
                hh, mm = divmod(age_s, 3600); mm, _ = divmod(mm, 60)
                age_txt = f"{hh}h{mm:02d}m 前同步"
            drift_txt = f"{drift_ms/1000:.2f}s" if abs(drift_ms) >= 1000 else f"{drift_ms:.0f}ms"
            sync_tag_cn = f"时间漂移 {drift_txt}{' ⚠️已暂停开仓' if drifted_pause else ''} · {age_txt}"
            sync_color = (
                "background:#fce8e6;color:#c5221f"
                if drifted_pause or abs(drift_ms) >= 5000
                else ("background:#feefc3;color:#8a6500" if abs(drift_ms) >= 1000
                      else "background:#e6f4ea;color:#137333")
            )
            time_sync_ctl = {
                "漂移毫秒": drift_ms, "漂移文本": drift_txt,
                "最后同步时间戳": last_sync_at or None,
                "同步距今年代": age_txt, "是否因漂移暂停": drifted_pause,
                "顶部标签文本": sync_tag_cn, "顶部标签颜色": sync_color,
            }
        drift_tag_text = str(time_sync_ctl.get("顶部标签文本") or "时间漂移 未同步")
        drift_tag_color = str(time_sync_ctl.get("顶部标签颜色") or "background:#e0e0e0;color:#3c4043")
        drift_ms_val = int(time_sync_ctl.get("漂移毫秒") or 0)
        drift_age_txt = str(time_sync_ctl.get("同步距今年代") or "未同步")
        drift_paused = bool(time_sync_ctl.get("是否因漂移暂停"))

        # 7) 最近交易：优先 journal；Controller 可用时直接 get_recent_trades
        trades_rows: list[dict] = []
        try:
            if ctl is not None and hasattr(ctl, "get_recent_trades"):
                records = list(ctl.get_recent_trades(limit=20) or [])
                for r in records:
                    trades_rows.append({
                        "时间": r.get("时间") or r.get("time") or "—",
                        "市场状态": r.get("市场状态") or r.get("market_regime") or "—",
                        "置信度": r.get("置信度") or r.get("confidence") or 0,
                        "入场原因": r.get("入场原因") or r.get("entry_reason") or "—",
                        "结果": r.get("结果") or r.get("result") or "—",
                    })
            else:
                storage = rt.get("storage")
                journal = storage[1] if isinstance(storage, tuple) and len(storage) >= 2 else None
                if journal is not None and hasattr(journal, "read_recent"):
                    records = list(journal.read_recent(limit=20) or [])
                    for r in records:
                        trades_rows.append({
                            "时间": r.get("timestamp") or r.get("时间") or "—",
                            "市场状态": r.get("market_regime") or r.get("市场状态") or "—",
                            "置信度": r.get("confidence") or r.get("置信度") or 0,
                            "入场原因": r.get("reason_short") or r.get("入场原因") or "—",
                            "结果": r.get("result_r") or r.get("结果") or "—",
                        })
        except Exception:
            trades_rows = []

        return {
            "mode": mode_cn,
            "status": sys_status_cn,
            "bal_total": total, "bal_available": available, "bal_upl": upl,
            "daily_start": daily_start,
            "total_pnl_pct": total_pnl_pct,
            "wins": wins, "losses": losses, "closed": closed, "wr": wr,
            "consec": consec, "allow": allow_val, "daily_status": daily_status,
            "daily_loss_pct": daily_loss_pct,
            "pos_side": pos_side, "pos_size": pos_size, "pos_entry": pos_entry,
            "pos_mark": pos_mark, "pos_upl": pos_upl,
            "protect_txt": protect_txt,
            "ai_regime": str(ai_block.get("市场状态") or "—"),
            "ai_conf": int(ai_block.get("置信度") or 0),
            "ai_direction": direction,
            "ai_reason": reason_short,
            # 2026-08-30 新增：AI 节流 7 级状态机彩色 tag 信息
            "thr_level": thr_level,
            "thr_color": thr_color,
            "thr_countdown": thr_countdown,
            "thr_daily_calls": thr_daily_calls,
            "thr_early_wakes": thr_early_wakes,
            "thr_failures": thr_failures,
            "thr_volatility": thr_volatility,
            "thr_reason": thr_reason,
            # 启动时间 + 运行时长（修复 started_at / uptime_seconds 此前一直为 None）
            "start_epoch": start_epoch,
            "start_local": start_local or "未启动",
            "uptime_seconds": upt_s or 0,
            "uptime_human": upt_human or "0s",
            # 2026-08-30 顶部漂移 tag（替代原 实盘模式 + AI 节流 顶部 tag）
            "drift_tag_text": drift_tag_text,
            "drift_tag_color": drift_tag_color,
            "drift_ms": drift_ms_val,
            "drift_age": drift_age_txt,
            "drift_paused": drift_paused,
            # 2026-08-30：运行模式卡『风控提示』（解释为什么一直不开仓：风控拒因/AI信号状态/缺口本金）
            # 2026-08-31：优先 ShadowBroker 虚拟持仓 → 真有持仓时显示「持仓概况」，不再 stale 显示「双过 Ns前 等成交」
            "risk_tip": _build_risk_tip(
                last_risk=rsk_ctl.get("最近一次风控"),
                pass_ts=int(rsk_ctl.get("最近一次交易信号就绪时间戳") or 0),
                allow_val=allow_val,
                ai_regime=(ai_block or {}).get("市场状态"),
                ai_conf=int((ai_block or {}).get("置信度") or 0),
                has_pos=(pos_side != "空仓") and float(pos_size or 0.0) > 0,
                pos_side_cn=pos_side,
                pos_size=float(pos_size or 0.0),
                pos_entry=float(pos_entry or 0.0),
                pos_mark=float(pos_mark or 0.0),
                pos_leverage=int(pos_leverage or 1),
                pos_upl=float(pos_upl or 0.0),
            ),
            "trades": trades_rows,
        }

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, summary="仪表盘首页")
    async def index(request: Request) -> str:
        """中文仪表盘首页（Dashboard 全部使用中文）—— 服务端直出骨架，JS 刷新。"""
        import json as _json
        d = await _collect_dashboard_data(request.app.state.runtime)
        # 2026-08-30：顶部不再显示 实盘模式 + AI 节流，改显示 {时间漂移} tag
        drift_tag = (
            f'<span id="k-drift-tag" class="tag" style="{d["drift_tag_color"]}">'
            f'{d["drift_tag_text"]}</span>'
        )
        thr_countdown_min, thr_countdown_s = divmod(max(int(d["thr_countdown"]), 0), 60)
        thr_countdown_str = f"{thr_countdown_min}m{thr_countdown_s:02d}s" if thr_countdown_min else f"{thr_countdown_s}s"

        # 最近交易表格行
        rows_html = ""
        if not d["trades"]:
            rows_html = '<tr><td colspan="5" style="text-align:center;color:#888;">暂无交易记录</td></tr>'
        else:
            for r in d["trades"][:20]:
                rows_html += (
                    f"<tr><td>{r.get('时间','—')}</td>"
                    f"<td>{r.get('市场状态','—')}</td>"
                    f"<td>{r.get('置信度',0)}</td>"
                    f"<td>{r.get('入场原因','—')}</td>"
                    f"<td>{r.get('结果','—')}</td></tr>"
                )

        # 注入 JSON 数据源，便于 JS 直接用（也能 SEO）
        data_json = _json.dumps(d, ensure_ascii=False, default=str)

        return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="UTF-8"/>
          <meta name="viewport" content="width=device-width,initial-scale=1"/>
          <title>云龙挑战赛 · Dashboard</title>
          <style>
            body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
                   padding:32px; background:#f6f7fb; color:#222; }}
            h1 {{ margin:0 0 16px; font-size:24px; }}
            h2 {{ font-size:16px; margin:0 0 14px; color:#334155; }}
            .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; margin-bottom:16px; }}
            .card {{ background:#fff; border-radius:12px; padding:18px 20px; box-shadow:0 2px 10px rgba(15,23,42,.05); }}
            .kv {{ display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; }}
            .kv .label {{ color:#64748b; font-size:12px; }}
            .kv .value {{ font-size:15px; font-weight:600; text-align:right; }}
            /* 2026-08-30: 风控提示跨两列显示（长文本需要整行）*/
            .kv .risk-tip-label {{ color:#64748b; font-size:12px; grid-column:1/2; }}
            .kv .risk-tip-value {{ grid-column:2/3; text-align:left; font-weight:500; font-size:12.5px; word-break:break-word; line-height:1.55; border-left:3px solid #cbd5e1; padding:4px 10px; background:#fafbfc; border-radius:4px; }}
            .kv .risk-tip-value.risk-deny {{ border-left-color:#c5221f; background:#fce8e6; color:#5f0e0b; }}
            .kv .risk-tip-value.risk-wait {{ border-left-color:#f1b000; background:#fff7d6; color:#5a4400; }}
            .kv .risk-tip-value.risk-pass {{ border-left-color:#137333; background:#e6f4ea; color:#083d18; }}
            .tag {{ display:inline-block; padding:2px 10px; border-radius:999px;
                     background:#e6f4ea; color:#137333; font-size:12px; margin-left:8px; vertical-align:middle; }}
            .tag.live {{ background:#fce8e6; color:#c5221f; }}
            .tag.warn {{ background:#feefc3; color:#8a6500; }}
            table {{ width:100%; border-collapse:collapse; }}
            th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #eef2f7; font-size:13px; }}
            th {{ color:#475569; font-weight:500; background:#fafbfc; }}
            a {{ color:#1a73e8; }}
            .loss {{ color:#c5221f; }}
            .win {{ color:#137333; }}
            footer {{ color:#888; font-size:12px; margin-top:20px; text-align:center; }}
            .updated {{ display:inline-block; margin-left:12px; font-size:12px; color:#64748b; }}
            .reason-box {{
              display:block; width:100%; font-size:12px; color:#334155;
              text-align:left; word-break:break-word; line-height:1.5;
              background:#f8fafc; padding:6px 8px; border-radius:6px; margin-top:4px;
            }}
          </style>
        </head>
        <body>
          <h1>云龙挑战赛 · Dashboard {drift_tag}<span id="k-updated" class="updated"></span></h1>

          <div class="grid">
            <!-- 运行模式 -->
            <div class="card">
              <h2>运 行 模 式</h2>
              <div class="kv">
                <div class="label">运行模式</div><div id="k-mode" class="value">{d['mode']}</div>
                <div class="label">系统状态</div><div id="k-status" class="value">{d['status']}</div>
                <div class="label">启动时间</div><div id="k-started" class="value">{d['start_local']}</div>
                <div class="label">运行时长</div><div id="k-uptime" class="value">{d['uptime_human']}</div>
                <div class="label">累计收益率 (%)</div><div id="k-total-pnl" class="value {'loss' if d['total_pnl_pct']<0 else 'win'}">{d['total_pnl_pct']:.2f}%</div>
                <div class="label">已平仓交易</div><div id="k-closed" class="value">{d['closed']} 笔（胜{d['wins']}/败{d['losses']}）</div>
                <div class="label risk-tip-label">风控提示</div>
                <div id="k-risk-tip" class="value risk-tip-value">{d['risk_tip']}</div>
              </div>
            </div>

            <!-- AI 节流（2026-08-30 新增：7 级状态机 + 价格哨兵可视化） -->
            <div class="card">
              <h2>AI 节 流 · 价 格 哨 兵</h2>
              <div class="kv">
                <div class="label">节流级别</div><div id="k-thr-level" class="value">{d['thr_level']}</div>
                <div class="label">下次调用倒计时</div><div id="k-thr-countdown" class="value">{thr_countdown_str}</div>
                <div class="label">当日 AI 调用次数</div><div id="k-thr-calls" class="value">{d['thr_daily_calls']} 次</div>
                <div class="label">早叫触发次数(≥1%)</div><div id="k-thr-early" class="value">{d['thr_early_wakes']} 次</div>
                <div class="label">最近 1m 价格波动</div><div id="k-thr-vol" class="value">{d['thr_volatility']:.2f}%</div>
                <div class="label">AI 连续失败</div><div id="k-thr-fail" class="value">{d['thr_failures']} 次</div>
                <div class="label">级别原因</div>
                <div id="k-thr-reason" class="value" style="grid-column:1/-1;"><span class="reason-box">{d['thr_reason']}</span></div>
              </div>
            </div>

            <!-- 余额 -->
            <div class="card">
              <h2>余 额</h2>
              <div class="kv">
                <div class="label">总权益 (USDT)</div><div id="k-bal-total" class="value">{d['bal_total']:.2f}</div>
                <div class="label">可用保证金</div><div id="k-bal-avail" class="value">{d['bal_available']:.2f}</div>
                <div class="label">未实现盈亏</div><div id="k-bal-upl" class="value {'loss' if d['bal_upl']<0 else 'win'}">{'+' if d['bal_upl']>=0 else ''}{d['bal_upl']:.2f}</div>
                <div class="label">今日起始权益</div><div id="k-daily-start" class="value">{d['daily_start']:.2f}</div>
              </div>
            </div>

            <!-- 风控 -->
            <div class="card">
              <h2>风 控</h2>
              <div class="kv">
                <div class="label">是否允许开仓</div><div id="k-allow" class="value">{d['allow']}</div>
                <div class="label">连续亏损</div><div id="k-consec" class="value">{d['consec']} 次</div>
                <div class="label">今日盈亏率</div><div id="k-daily-loss" class="value {'loss' if d['daily_loss_pct']<0 else ''}">{'+' if d['daily_loss_pct']>=0 else ''}{d['daily_loss_pct']:.2f}%</div>
                <div class="label">胜率</div><div id="k-wr" class="value">{d['wr']:.0f}%</div>
              </div>
            </div>

            <!-- 持仓 -->
            <div class="card">
              <h2>持 仓</h2>
              <div class="kv">
                <div class="label">持仓方向</div><div id="k-pos-side" class="value">{d['pos_side']}</div>
                <div class="label">数量</div><div id="k-pos-size" class="value">{d['pos_size']:.6f}</div>
                <div class="label">开仓均价 / 标记价</div><div id="k-pos-price" class="value">{d['pos_entry']:.2f} / {d['pos_mark']:.2f}</div>
                <div class="label">浮动盈亏 / 保护</div><div id="k-pos-upl" class="value">{'+' if d['pos_upl']>=0 else ''}{d['pos_upl']:.2f} · {d['protect_txt']}</div>
              </div>
            </div>

            <!-- AI 判断 -->
            <div class="card">
              <h2>AI 判 断</h2>
              <div class="kv">
                <div class="label">市场状态</div><div id="k-ai-regime" class="value">{d['ai_regime']}</div>
                <div class="label">置信度</div><div id="k-ai-conf" class="value">{d['ai_conf']:.0f}%</div>
                <div class="label">建议方向</div><div id="k-ai-dir" class="value">{d['ai_direction']}</div>
                <div class="label">简短理由</div>
                <div id="k-ai-reason" class="value" style="grid-column:1/-1;"><span class="reason-box">{d['ai_reason']}</span></div>
              </div>
            </div>
          </div>

          <div class="card">
            <h2>最 近 交 易</h2>
            <table>
              <thead><tr>
                <th>时间</th><th>市场状态</th><th>置信度</th><th>入场原因</th><th>结果</th>
              </tr></thead>
              <tbody id="k-trades">{rows_html}</tbody>
            </table>
          </div>

          <footer>by 李云龙</footer>

          <script>
            // 每 5s 刷新：读 /api/status + /api/trades → 写对应 id DOM
            // 失败（Controller 未初始化 / 网络）时静默，绝不刷新成 0 值
            window.__DASHBOARD__ = {{data: {data_json}}};

            function setText(id, text, {{loss=false, win=false, forceLossClass=false}} = {{}}) {{
              const el = document.getElementById(id);
              if (!el || text === null || text === undefined) return;
              el.textContent = String(text);
              el.classList.remove('loss','win');
              if (forceLossClass || (typeof loss === 'number' && loss < 0)) el.classList.add('loss');
              if (win && (typeof win !== 'number' || win > 0)) el.classList.add('win');
            }}

            function fmtSigned(v, decimals=2, suffix='') {{
              const n = Number(v);
              if (Number.isNaN(n)) return String(v ?? '—');
              const sign = n >= 0 ? '+' : '';
              return sign + n.toFixed(decimals) + suffix;
            }}

            async function refreshStatus() {{
              try {{
                const resp = await fetch('/api/status', {{cache:'no-store'}});
                if (!resp.ok) return;
                const s = await resp.json();
                // 运行模式 / 状态
                setText('k-mode', s['运行模式'] ?? null);
                setText('k-status', s['系统状态'] ?? null);
                // 启动时间 + 运行时长（修复 started_at/uptime_seconds 此前为 None）
                setText('k-started', s['启动时间'] ?? s['start_local'] ?? '未启动');
                const upS = Number(s['运行时长(秒)'] ?? s['uptime_seconds'] ?? 0);
                let upTxt = s['运行时长'] ?? null;
                if (!upTxt) {{
                  const h = Math.floor(upS / 3600), m = Math.floor((upS % 3600) / 60), s2 = Math.floor(upS % 60);
                  upTxt = h > 0 ? (h + 'h' + String(m).padStart(2,'0') + 'm' + String(s2).padStart(2,'0') + 's')
                         : m > 0 ? (m + 'm' + String(s2).padStart(2,'0') + 's') : (s2 + 's');
                }}
                setText('k-uptime', upTxt);
                const totalPnl = Number(s['累计收益率(%)'] ?? 0);
                const elPnl = document.getElementById('k-total-pnl');
                if (elPnl) {{
                  elPnl.textContent = fmtSigned(totalPnl, 2, '%');
                  elPnl.classList.remove('loss','win');
                  elPnl.classList.add(totalPnl < 0 ? 'loss' : 'win');
                }}
                const wins = Number(s['盈利次数'] ?? 0);
                const losses = Number(s['亏损次数'] ?? 0);
                const closed = wins + losses;
                setText('k-closed', closed + ' 笔（胜' + wins + '/败' + losses + '）');

                // 余额
                const balTotal = Number(s['账户余额总权益'] ?? 0);
                const balAvail = Number(s['可用保证金'] ?? 0);
                const balUpl = Number(s['未实现盈亏'] ?? 0);
                setText('k-bal-total', balTotal.toFixed(2));
                setText('k-bal-avail', balAvail.toFixed(2));
                setText('k-bal-upl', fmtSigned(balUpl, 2), {{forceLossClass: balUpl < 0}});

                // 风控
                const risk = s['风控状态'] || {{}};
                setText('k-allow', risk['是否允许开仓'] ?? null);
                setText('k-consec', (risk['连续亏损次数'] ?? 0) + ' 次');
                const wr = closed > 0 ? (wins / closed * 100) : 0;
                setText('k-wr', wr.toFixed(0) + '%');

                // 2026-08-31 修复 Bug B：持仓卡 5s 刷新必须走 /api/status['当前持仓']
                //   （SSR 启动时确实读了 broker 实时，但若 ShadowBroker 之后开了空仓，
                //    不刷新 DOM 用户就一直看到「空仓 / 0.000000」——用户现场就是这情况！）
                const curPos = s['当前持仓'] || {{}};
                const pSide = String(curPos['方向'] ?? '空仓');
                const pSize = Number(curPos['数量'] ?? 0);
                const pEntry = Number(curPos['开仓均价'] ?? 0);
                const pMark = Number(curPos['标记价'] ?? 0);
                const pUpl = Number(curPos['未实现盈亏'] ?? 0);
                const pLev = Number(curPos['杠杆'] ?? 1);
                setText('k-pos-side', pSide);
                const elSz = document.getElementById('k-pos-size');
                if (elSz) elSz.textContent = pSize.toFixed(6);
                const elPE = document.getElementById('k-pos-price') || document.getElementById('k-pos-entry');
                if (elPE) elPE.textContent = pEntry.toFixed(2) + ' / ' + pMark.toFixed(2);
                const elPU = document.getElementById('k-pos-upl');
                if (elPU) {{
                  const protectTxt = (elPU.textContent || '').split('·')[1] || '· 未启用';
                  const uplPrefix = (pUpl >= 0 ? '+' : '') + pUpl.toFixed(2);
                  elPU.textContent = uplPrefix + protectTxt.replace(/^(\\s*·)?/, ' · ');
                  elPU.classList.remove('loss','win');
                  elPU.classList.add(pUpl < 0 ? 'loss' : 'win');
                }}
                // 把实时持仓同步到 window.__ycsLastFull（如果没有的话），方便风控提示卡拿
                if (!window.__ycsLastFull) window.__ycsLastFull = {{}};
                if (!window.__ycsLastFull.position) window.__ycsLastFull.position = {{}};
                window.__ycsLastFull.position.side = pSide;
                window.__ycsLastFull.position.size = pSize;
                window.__ycsLastFull.position.entry_price = pEntry;
                window.__ycsLastFull.position.mark_price = pMark;
                window.__ycsLastFull.position.unrealized_pnl = pUpl;
                window.__ycsLastFull.position.leverage = pLev;

                // 风控提示（2026-08-30 新增：直接显示『为什么不开仓』）
                // 2026-08-31 修复：若 state.position.size>0（或 state_store 持仓大小>0）→ 显示「已持仓概况」
                const elRiskTip = document.getElementById('k-risk-tip');
                if (elRiskTip) {{
                  const lastRisk = risk['最近一次风控'] || {{}};
                  const ai = s['最近AI判断'] || {{}};
                  const stPos = (window.__ycsLastFull && window.__ycsLastFull.position) ? window.__ycsLastFull.position : null;
                  const pos = stPos || {{}};
                  const posSize = Number(pos.size ?? (typeof setText === 'function' ? 0 : 0));
                  // 也从 /api/status 的持仓展示卡读：若 Dashboard 元素显示 size 非 0 也认为已持仓
                  let posSize2 = 0;
                  try {{
                    const elSz = document.getElementById('k-pos-size');
                    if (elSz && elSz.textContent) {{
                      const m = String(elSz.textContent).match(/([\\d.]+)/);
                      if (m) posSize2 = parseFloat(m[1]) || 0;
                    }}
                  }} catch(e) {{}}
                  const hasPosition = posSize > 0 || posSize2 > 0;
                  const aiRegime = String(ai['市场状态'] ?? '暂无');
                  const aiConf = Number(ai['置信度'] ?? 0);
                  const aiOK = (aiRegime === '上涨趋势' || aiRegime === '下跌趋势') && aiConf >= 50;
                  const passTs = Number(risk['最近一次交易信号就绪时间戳'] ?? 0);
                  const conc = String(lastRisk['结论'] ?? '未执行');
                  const reason = String(lastRisk['原因'] ?? '').slice(0, 120);
                  const gap = lastRisk['缺口本金(USDT)'];
                  const lev = lastRisk['建议杠杆(X)'];
                  const sugNom = lastRisk['建议名义价值(USDT)'];
                  const minNom = lastRisk['最小名义(USDT)'];
                  let tip = '';
                  let kind = ''; // ''/deny/wait/pass  ->  加 CSS class
                  if (hasPosition) {{
                    const side = pos.side || (posSize2>0 ? (document.getElementById('k-pos-side') || {{}}).textContent : '') || '多单';
                    const entry = Number(pos.entry_price ?? 0).toFixed(1);
                    const mark = Number(pos.mark_price ?? 0).toFixed(1);
                    const levTxt = Number(pos.leverage ?? 1) > 1 ? (' / ' + Number(pos.leverage ?? 1) + 'X') : '';
                    const upl = Number(pos.unrealized_pnl ?? 0);
                    const uplTxt = (upl >= 0 ? '+' : '') + upl.toFixed(4) + 'U';
                    tip = '📦 已持仓 ' + Math.max(posSize, posSize2).toFixed(1) + ' 张(' + side + ')｜成本 ' + entry + '$ / 现价 ' + mark + '$' + levTxt + '｜浮盈亏 ' + uplTxt + '｜后续：AI 风控触发利润保护平仓';
                    kind = 'pass';
                  }} else if (passTs > 0 && conc === '通过') {{
                    const age = Math.floor(Date.now()/1000 - passTs);
                    const h = Math.floor(age/3600), m = Math.floor((age%3600)/60), s2 = age%60;
                    const a = (h>0? h+'h'+String(m).padStart(2,'0')+'m'+String(s2).padStart(2,'0')+'s前'
                              : (m>0? m+'m'+String(s2).padStart(2,'0')+'s前' : s2+'s前'));
                    tip = '✅ 信号双过 ' + a + '，等成交/下一轮再评估 · 风控通过(名义 ' + sugNom + 'U ≥ min ' + minNom + 'U @ ' + lev + 'X) · AI[' + aiRegime + ' conf=' + aiConf + ']';
                    kind = 'pass';
                  }} else if (conc === '拒绝') {{
                    let tail = '';
                    if (gap != null && !isNaN(Number(gap)) && Number(gap) > 0) {{
                      tail = ' · ⚠️还差 ≈ ' + Number(gap).toFixed(2) + 'U 本金摸到最小单；调杠杆(当前 ' + lev + 'X)或调大R%/止损%';
                    }}
                    tip = '❌ 风控拒绝 · ' + reason + tail;
                    kind = 'deny';
                  }} else if (conc === '通过') {{
                    if (!aiOK) {{
                      tip = '🟡 风控通过(名义 ' + sugNom + 'U ≥ min ' + minNom + 'U @ ' + lev + 'X) · 等AI信号(当前 [' + aiRegime + ' conf=' + aiConf + ']，需 TREND_UP/DOWN + ≥50)';
                      kind = 'wait';
                    }} else {{
                      tip = '🟢 风控通过 + AI[' + aiRegime + ' conf=' + aiConf + '] · 信号应已发送，查「最近交易」或 journalctl -u ycs | grep [主循环]';
                      kind = 'pass';
                    }}
                  }} else {{
                    tip = '启动中：等待第一轮风控评估（10s 内）';
                  }}
                  elRiskTip.textContent = tip;
                  elRiskTip.classList.remove('risk-pass','risk-deny','risk-wait');
                  if (kind === 'deny') elRiskTip.classList.add('risk-deny');
                  else if (kind === 'wait') elRiskTip.classList.add('risk-wait');
                  else if (kind === 'pass') elRiskTip.classList.add('risk-pass');
                }}

                // AI
                const ai = s['最近AI判断'] || {{}};
                setText('k-ai-regime', ai['市场状态'] ?? null);
                setText('k-ai-conf', (Number(ai['置信度'] ?? 0)).toFixed(0) + '%');
                // 建议方向：推断（兜底）
                let dir = ai['建议方向'] || null;
                if (!dir || dir === '—') {{
                  const regime = String(ai['市场状态'] ?? '');
                  if (regime.includes('上涨')) dir = '做多 LONG';
                  else if (regime.includes('下跌')) dir = '做空 SHORT';
                  else if (regime.includes('震荡') || regime.includes('低波动')) dir = '观望 WAIT';
                  else if (regime.includes('波动')) dir = '谨慎观望';
                  else dir = '—';
                }}
                setText('k-ai-dir', dir);
                const reason = String(ai['理由'] ?? '暂无');
                const elR = document.getElementById('k-ai-reason');
                if (elR) elR.innerHTML = '<span class="reason-box">' + (reason.length > 120 ? reason.slice(0,120)+'…' : reason) + '</span>';

                // AI 节流（2026-08-30：仅卡片显示，顶部不再挂 tag；节流级别仅更新卡片内部）
                const thr = s['AI节流状态'] || {{}};
                const thrLevel = String(thr['节流级别'] ?? 'NORMAL');
                setText('k-thr-level', thrLevel);
                const cd = Number(thr['倒计时(秒)'] ?? 0);
                const mm = Math.floor(cd / 60), ss = Math.floor(cd % 60);
                setText('k-thr-countdown', mm > 0 ? (mm + 'm' + String(ss).padStart(2,'0') + 's') : (ss + 's'));
                setText('k-thr-calls', (thr['当日调用次数'] ?? 0) + ' 次');
                setText('k-thr-early', (thr['当日早叫次数'] ?? 0) + ' 次');
                setText('k-thr-vol', (Number(thr['最近波动(%)'] ?? 0)).toFixed(2) + '%');
                setText('k-thr-fail', (thr['连续失败次数'] ?? 0) + ' 次');
                const thrReason = String(thr['级别原因'] ?? '');
                const elThrR = document.getElementById('k-thr-reason');
                if (elThrR) elThrR.innerHTML = '<span class="reason-box">' + (thrReason.length > 80 ? thrReason.slice(0,80)+'…' : thrReason) + '</span>';

                // 顶部时间漂移 tag（2026-08-30 按需求：替代原 实盘模式+AI节流顶部）
                const ts = s['时间同步状态'] || {{}};
                const driftTagEl = document.getElementById('k-drift-tag');
                if (driftTagEl) {{
                    const tagText = String(ts['顶部标签文本'] ?? '时间漂移 未同步');
                    const tagColor = String(ts['顶部标签颜色'] ?? 'background:#e0e0e0;color:#3c4043');
                    driftTagEl.textContent = tagText;
                    driftTagEl.setAttribute('style', tagColor);
                }}

                // 更新时间
                const up = document.getElementById('k-updated');
                if (up) up.textContent = '· 最近刷新 ' + new Date().toLocaleTimeString();
              }} catch (_) {{ /* 未注入 controller 时静默 */ }}
            }}

            async function refreshTrades() {{
              try {{
                const resp = await fetch('/api/trades?limit=20', {{cache:'no-store'}});
                if (!resp.ok) return;
                const rows = await resp.json();
                const tbody = document.getElementById('k-trades');
                if (!tbody) return;
                if (!rows || !Array.isArray(rows) || rows.length === 0) {{
                  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#888;">暂无交易记录</td></tr>';
                  return;
                }}
                tbody.innerHTML = rows.slice(0,20).map(r => (
                  '<tr>' +
                    '<td>' + (r['时间'] ?? r['time'] ?? '—') + '</td>' +
                    '<td>' + (r['市场状态'] ?? r['market_regime'] ?? '—') + '</td>' +
                    '<td>' + (r['置信度'] ?? r['confidence'] ?? 0) + '</td>' +
                    '<td>' + (r['入场原因'] ?? r['entry_reason'] ?? '—') + '</td>' +
                    '<td>' + (r['结果'] ?? r['result'] ?? '—') + '</td>' +
                  '</tr>'
                )).join('');
              }} catch (_) {{ /* 未注入 controller 时静默 */ }}
            }}

            async function refresh() {{
              await Promise.all([refreshStatus(), refreshTrades()]);
            }}
            // 首次 2s 后触发（等 Controller 初始化完）；之后每 5s
            setTimeout(refresh, 2000);
            setInterval(refresh, 5000);
          </script>
        </body>
        </html>
        """

    @app.get("/api/health", summary="健康检查")
    async def health() -> dict:
        return {"ok": True, "message": "云龙挑战赛系统运行中"}

    # ------------------------------------------------------------------
    # 中文业务 API
    # ------------------------------------------------------------------
    @app.get("/api/status", summary="系统总览（运行模式/状态/AI/风控）")
    async def api_status(request: Request) -> dict:
        ctl = request.app.state.runtime.get("controller")
        if ctl is not None:
            return ctl.get_status_dict()

        # 兜底：Controller 尚未初始化（如：仅 create_app 跑骨架、或前端刚打开 Dashboard）。
        # 仍保证"运行模式"字段可被监控脚本 / 自检读到——这是影子模式判定的关键入口。
        # 2026-08-30: started_at / uptime 空值修复：即使 Controller 没初始化，也从 state_store 推算（3 处写入路径统一）
        import time as _t2, datetime as _dt2  # noqa: PLC0415
        start_epoch: int | None = None
        start_local = None
        upt_seconds = None
        upt_human = None
        store = request.app.state.runtime.get("state_store")
        if store is None:
            storages = request.app.state.runtime.get("storage")
            if isinstance(storages, (list, tuple)) and len(storages) > 0:
                store = storages[0]
        if store is not None and hasattr(store, "load"):
            try:
                raw_sa = (store.load() or {}).get("started_at")
                if isinstance(raw_sa, int) and raw_sa > 0:
                    start_epoch = raw_sa
                elif isinstance(raw_sa, str):
                    try:
                        start_epoch = int(_dt2.datetime.strptime(raw_sa, "%Y-%m-%d %H:%M:%S").timestamp())
                    except Exception:  # noqa: BLE001
                        start_epoch = None
            except Exception:  # noqa: BLE001
                pass
        if start_epoch:
            try:
                start_local = _t2.strftime("%Y-%m-%d %H:%M:%S", _t2.localtime(int(start_epoch)))
            except Exception:  # noqa: BLE001
                start_local = None
            s_ = max(int(_t2.time()) - int(start_epoch), 0)
            upt_seconds = s_
            hh, rem = divmod(s_, 3600); mm, ss = divmod(rem, 60)
            upt_human = (f"{hh}h{mm:02d}m{ss:02d}s" if hh > 0 else (f"{mm}m{ss:02d}s" if mm > 0 else f"{ss}s"))

        cfg = request.app.state.runtime.get("config")
        mode_cn = "纸盘模式"
        shadow = False
        if cfg is not None and hasattr(cfg, "trading"):
            mode_cn = "实盘模式" if bool(getattr(cfg.trading, "live", False)) else "纸盘模式"
        if cfg is not None and hasattr(cfg, "risk_limits"):
            shadow = bool(getattr(cfg.risk_limits, "shadow_mode", False))
        if shadow:
            mode_cn = f"{mode_cn}(影子 SHADOW)"
        # AI 节流默认值（兜底）
        from app.core.ai_throttle import LEVEL_COLORS as _THR_COLORS  # noqa: PLC0415
        _lvl = "NORMAL"
        return {
            "运行模式": mode_cn,
            "系统状态": "未初始化（等待 run.py 注入 TradingController）",
            "启动时间戳(epoch秒)": start_epoch,
            "启动时间": start_local,
            "运行时长(秒)": upt_seconds,
            "运行时长": upt_human,
            "账户余额总权益": 0.0,
            "可用保证金": 0.0,
            "未实现盈亏": 0.0,
            "累计交易次数": 0,
            "盈利次数": 0,
            "亏损次数": 0,
            "累计收益率(%)": 0,
            "最近AI判断": {
                "市场状态": "暂无",
                "置信度": 0,
                "理由": "Controller 未初始化",
                "时间": None,
            },
            "AI节流状态": {
                "节流级别": _lvl,
                "节流颜色": _THR_COLORS.get(_lvl, "background:#e6f4ea;color:#137333"),
                "级别原因": "Controller 未初始化（系统启动中）",
                "倒计时(秒)": 0,
                "下次调用时间戳": None,
                "当日调用次数": 0,
                "当日成本(估USDT)": 0,
                "当日早叫次数": 0,
                "连续失败次数": 0,
                "连续成功次数": 0,
                "哨兵锚定价": 0,
                "最近波动(%)": 0,
                "最近波动时间戳": None,
            },
            "时间同步状态": {
                "漂移毫秒": 0,
                "漂移文本": "0ms",
                "最后同步时间戳": None,
                "同步距今年代": "未同步",
                "是否因漂移暂停": False,
                "顶部标签文本": "时间漂移 未同步 · 启动中",
                "顶部标签颜色": "background:#e0e0e0;color:#3c4043",
                "阈值秒": 10,
            },
            "风控状态": {
                "连续亏损次数": 0,
                "熔断冷却至(秒时间戳)": 0,
                "是否允许开仓": "否",
                "最近一次风控": {
                    "时间戳": None, "结论": "未执行", "原因": "Controller 未初始化（系统启动中）",
                    "建议杠杆(X)": None, "建议名义价值(USDT)": None, "最小名义(USDT)": None,
                    "缺口本金(USDT)": None, "AI_信号状态": "暂无",
                },
                "最近一次交易信号就绪时间戳": 0,
            },
        }

    @app.get("/api/balance", summary="账户余额（USDT）")
    async def api_balance(request: Request) -> dict:
        ctl = _get_controller(request)
        import asyncio
        return await ctl.get_balance_dict()

    @app.get("/api/position", summary="当前持仓")
    async def api_position(request: Request) -> dict:
        ctl = _get_controller(request)
        import asyncio
        return await ctl.get_position_dict()

    @app.get("/api/trades", summary="最近交易流水")
    async def api_trades(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500, description="返回条数，默认 50"),
    ) -> list:
        ctl = _get_controller(request)
        return ctl.get_recent_trades(limit=limit)

    @app.get("/api/ai/analyze", summary="触发一次 AI 市场分析（实盘路径）")
    async def api_ai_analyze(request: Request) -> dict:
        """调用 TradingController.analyze()：
           读取 Broker 实时行情 / 纸盘快照 → 调 AIAnalyzer（AI不可用则走离线规则兜底）
           → 返回建议方向/入场/止损/止盈/理由。

        2026-08-29 用户明确「抓紧上实盘，离线 fixtures 多余」：
          · 不再接受 ?fixture=trend_up 等离线参数，离线 K 线文件、加载器、
            18×CSV.GZ 目录均已删除。
          · 真要离线分析旧行情，请直接用真实实盘环境 /api/diag 抓 controller.analyze
            的实时输出，不要依赖离线 CSV。
        """
        ctl = _get_controller(request)
        return await ctl.analyze()

    # ------------------------------------------------------------------
    # A5. Kill-Switch HTTP 通道（POST /api/kill）：紧急全平 + 停机
    # ------------------------------------------------------------------
    @app.post("/api/kill", summary="【紧急】Kill-Switch：撤所有挂单→市价全平→写入 STOP 状态→熔断24h")
    async def api_kill(
        request: Request,
        x_ycs_admin_token: str = Header(
            default="",
            description="紧急停机口令：必须等于配置 risk_limits.kill_switch_token。其它通道：ycsctl kill、data/EMERGENCY_HALT 文件。",
            alias="X-YCS-Admin-Token",
        ),
    ):
        """三通道之一：HTTP POST。其它通道：ycsctl kill、data/EMERGENCY_HALT 文件。
           安全：必须带 X-YCS-Admin-Token 头 == config.risk_limits.kill_switch_token。
        """
        from fastapi import status as _st
        rt = request.app.state.runtime or {}
        cfg = rt.get("config")
        expected_token = ""
        if cfg is not None and hasattr(cfg, "risk_limits"):
            expected_token = str(getattr(cfg.risk_limits, "kill_switch_token", "") or "")
        got_token = str(x_ycs_admin_token or request.headers.get("x-ycs-admin-token") or "")
        if expected_token and got_token != expected_token:
            raise HTTPException(
                status_code=_st.HTTP_401_UNAUTHORIZED,
                detail="X-YCS-Admin-Token 不匹配（配置 risk_limits.kill_switch_token）",
            )

        ctl = rt.get("controller")
        result_payload: dict[str, Any] = {"ok": True, "actions": []}

        # 动作①：把 state_store 标成 STOPPED + 熔断 24 小时
        store = rt.get("state_store")
        if store is not None and hasattr(store, "load"):
            try:
                st = store.load() or {}
                from app.core.constants import SystemStatus as _SS  # noqa: PLC0415
                st["status"] = _SS.STOPPED.value
                st["stopped_by"] = "kill-switch-http"
                st["stopped_at"] = int(__import__("time").time())
                risk_dict = st.get("risk") or {}
                risk_dict["cooldown_until"] = st["stopped_at"] + 86_400
                risk_dict["allow_trading"] = False
                st["risk"] = risk_dict
                store.save(st)
                result_payload["actions"].append("state_store: STOPPED + cooldown 24h")
            except Exception as e:  # noqa: BLE001
                result_payload["actions"].append(f"state_store: ERROR {type(e).__name__}: {e}")

        # 动作②：若 Controller 可用 → 撤所有挂单 + 市价全平当前仓位（PaperBroker 也走模拟全平）
        if ctl is not None and hasattr(ctl, "broker"):
            try:
                sym = getattr(ctl.config.trading, "symbol", None) or "ETH-USDT-SWAP"
                broker = ctl.broker
                # 撤所有挂单
                open_orders = await broker.get_open_orders(sym)
                for o in open_orders or []:
                    try:
                        await broker.cancel_order(sym, o.client_order_id)
                    except Exception:
                        pass
                result_payload["actions"].append(f"broker: canceled {len(open_orders or [])} open orders")
                # 全平
                from app.core.constants import (  # noqa: PLC0415
                    OrderSide as _OS, OrderType as _OT, PositionSide as _PS,
                )
                pos = await broker.get_position(sym)
                if pos.side != _PS.FLAT and abs(float(pos.size or 0)) > 0:
                    side = _OS.SELL if pos.side == _PS.LONG else _OS.BUY
                    filled_order = await broker.place_order(
                        sym, side=side, type=_OT.MARKET, amount=abs(float(pos.size)),
                        client_order_id=None,
                    )
                    result_payload["actions"].append(
                        f"broker: closed {pos.side.value} × {float(pos.size)} via MARKET → {filled_order.status.value if hasattr(filled_order, 'status') else 'done'}"
                    )
                else:
                    result_payload["actions"].append("broker: no position to close")
            except Exception as e:  # noqa: BLE001
                result_payload["actions"].append(f"broker: ERROR {type(e).__name__}: {e}")
                result_payload["ok"] = False
        else:
            result_payload["actions"].append("broker: Controller/broker 未注入（仅写状态机 STOP）")

        # 动作③：写 data/EMERGENCY_HALT 兜底文件（双保险）
        try:
            from pathlib import Path as _P  # noqa: PLC0415
            import time as _t  # noqa: PLC0415
            project_root = _P(__file__).resolve().parent.parent.parent
            halt_path = project_root / "data" / "EMERGENCY_HALT"
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.write_text(
                f"created_by=api_kill_http\nat={int(_t.time())}\n",
                encoding="utf-8",
            )
            result_payload["actions"].append(f"wrote EMERGENCY_HALT: {halt_path}")
        except Exception as e:  # noqa: BLE001
            result_payload["actions"].append(f"EMERGENCY_HALT: ERROR {type(e).__name__}: {e}")
        return result_payload

    # ------------------------------------------------------------------
    # A6. GET /api/logs：通过 /docs Try-it-out 直接查看影子/交易日志（省 SSH）
    # ------------------------------------------------------------------
    _ALLOWED_LOG_FILES = ("trade", "system", "error")  # 不许任意路径穿越
    _LOG_FILE_MAP = {
        "trade": "trade.log",
        "system": "system.log",
        "error": "error.log",
    }

    def _safe_tail_read(p, max_chars: int) -> str:
        """从文件末尾读最多 max_chars 字符（避免读几 GB 的 rotated log）。"""
        try:
            import os as _os  # noqa: PLC0415
            size = p.stat().st_size
            if size <= max_chars:
                return p.read_text(encoding="utf-8", errors="replace")
            with open(p, "rb") as f:
                f.seek(size - max_chars, _os.SEEK_SET)
                # 丢掉可能不完整的首个多字节行（split 会从\n后面开始解析新行，这里无所谓第一个半截行）
                data = f.read(max_chars)
            return data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    def _read_journal_shell(n: int, kw: str) -> tuple[list[str], int, int]:
        """非 Windows：journalctl -u ycs -n n*5 → 再 filter 再 tail n。"""
        import subprocess as _sp  # noqa: PLC0415
        try:
            raw_bytes = _sp.check_output(
                ["journalctl", "-u", "ycs", "-n", str(max(1, int(n)) * 8), "--no-pager"],
                stderr=_sp.DEVNULL, timeout=6,
            )
            text = raw_bytes.decode("utf-8", errors="replace")
        except (FileNotFoundError, _sp.CalledProcessError, _sp.TimeoutExpired, OSError):
            return [], 0, 0
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if (kw or "").strip():
            matched = [ln for ln in lines if kw in ln]
            return matched, len(matched), len(matched[-max(1, int(n)):])
        return lines, len(lines), len(lines[-max(1, int(n)):])

    @app.get(
        "/api/logs",
        summary="查看影子/交易日志（默认 trade.log：含 [SHADOW] 影子成交序列）",
        response_description="tail 最近 N 行；可 filter 关键字过滤；默认文件=trade（影子成交日志）。鉴权：X-YCS-Admin-Token == risk_limits.kill_switch_token。",
    )
    async def api_logs(
        request: Request,
        file: str = "trade",  # 默认 trade.log：影子成交、AI 决策、风控拒单都在这里
        n: int = 200,
        filter: str = "",  # noqa: A002 — 保持 query param 名"filter"友好，/docs 一眼看懂
        x_ycs_admin_token: str = Header(
            default="",
            alias="X-YCS-Admin-Token",
            description="鉴权口令：必须等于配置 risk_limits.kill_switch_token（与 /api/kill 同口）。",
        ),
    ):
        """三文件：
        - **trade** :trade.log → 影子成交 [SHADOW]、AI 决策、风控拒单（影子联调最常用）
        - **system**: system.log → 启动/恢复/节流通用日志（排障用）
        - **error** : error.log → 仅 ERROR 级别（第一现场排查 fatal 用）

        也支持 file=journal → 调 journalctl -u ycs -n N（需 systemd）。
        """
        from fastapi import status as _st
        rt = request.app.state.runtime or {}
        cfg = rt.get("config")
        expected_token = ""
        if cfg is not None and hasattr(cfg, "risk_limits"):
            expected_token = str(getattr(cfg.risk_limits, "kill_switch_token", "") or "")
        got_token = str(x_ycs_admin_token or request.headers.get("x-ycs-admin-token") or "")
        if expected_token and got_token != expected_token:
            raise HTTPException(
                status_code=_st.HTTP_401_UNAUTHORIZED,
                detail="X-YCS-Admin-Token 不匹配（配置 risk_limits.kill_switch_token）",
            )
        # 1) 枚举校验
        file_norm = str(file or "").strip().lower().replace(".log", "")
        if file_norm == "journal":
            _kw = (filter or "").strip()
            _n = max(1, int(n))
            all_l, total_matched, _ret = _read_journal_shell(n=_n, kw=_kw)
            if _kw:
                matched = [ln for ln in all_l if _kw in ln]
                returned = matched[-_n:]
                return {
                    "file": "journal", "source": "systemd journalctl -u ycs",
                    "total_matched": len(matched), "returned": len(returned),
                    "filter_used": _kw or None, "n_requested": _n,
                    "lines": returned,
                }
            return {
                "file": "journal", "source": "systemd journalctl -u ycs",
                "total_matched": total_matched, "returned": min(total_matched, _n),
                "filter_used": None, "n_requested": _n,
                "lines": all_l[-_n:],
            }
        if file_norm not in _ALLOWED_LOG_FILES:
            raise HTTPException(
                status_code=_st.HTTP_400_BAD_REQUEST,
                detail=(
                    f"file 非法：{file!r}。"
                    f"允许值: {' / '.join(_ALLOWED_LOG_FILES)} / journal（systemd）。默认=trade。"
                ),
            )
        actual_file = _LOG_FILE_MAP[file_norm]

        # 2) 定位 logs_root：按优先级
        #   a) runtime["logs_root"]（测试注入）
        #   b) runtime["runtime_root"] / "logs"（create_app 常规参数）
        #   c) PROJECT_ROOT / "logs"（PROJECT_ROOT = app/api/app.py 的 parent.parent.parent）
        from pathlib import Path as _P2  # noqa: PLC0415
        logs_root = rt.get("logs_root") or None
        if logs_root:
            logs_root_p = _P2(str(logs_root))
        elif rt.get("runtime_root"):
            logs_root_p = _P2(str(rt["runtime_root"])) / "logs"
        else:
            logs_root_p = _P2(__file__).resolve().parent.parent.parent / "logs"
        target_path = logs_root_p / actual_file

        # 3) tail + 过滤
        all_lines: list[str] = []
        try:
            if target_path.exists() and target_path.is_file():
                raw = _safe_tail_read(target_path, max_chars=max(200_000, n * 800))
                all_lines = [ln.rstrip("\r\n") for ln in raw.split("\n") if ln != ""]
        except Exception:  # noqa: BLE001
            all_lines = []
        _f = (filter or "").strip()
        if _f:
            matched = [ln for ln in all_lines if _f in ln]
            total_matched = len(matched)
            returned_lines = matched[-max(1, int(n)):]
        else:
            total_matched = len(all_lines)
            returned_lines = all_lines[-max(1, int(n)):]
        return {
            "file": actual_file,
            "source": str(target_path),
            "total_matched": total_matched,
            "returned": len(returned_lines),
            "filter_used": _f or None,
            "n_requested": int(n),
            "lines": returned_lines,
        }

    # ------------------------------------------------------------------
    # B. GET /api/diag 诊断快照（供 AI 分析项目缺陷 / 用户远程自查）
    # ------------------------------------------------------------------
    @app.get("/api/diag", summary="诊断快照：返回 system/broker/controller/pm/journal/safety/fixtures/risks 8 大类结构化数据")
    async def api_diag(request: Request) -> dict[str, Any]:
        """输出结构化、键名稳定（可做 AI Prompt 直接粘）的系统快照，用于：
           ① 你直接 curl 贴过来给我分析缺陷；
           ② 自动化巡检脚本比对；
           ③ 实盘出事时的"黑匣子"快照。
        """
        import time as _t, os as _os, sys as _sys  # noqa: E401
        from pathlib import Path as _P  # noqa: E401

        rt = request.app.state.runtime or {}
        cfg = rt.get("config")
        ctl = rt.get("controller")
        store = rt.get("state_store")
        project_root = PROJECT_ROOT  # 模块级统一推导（跨平台/跨部署稳定）

        # ── 1) system 元信息 ──────────────────────────────────────
        version = "1.0.0"
        mode_cn = "纸盘模式"
        shadow = False
        max_eq = 15.0; max_daily_loss = 3.0
        if cfg is not None:
            if hasattr(cfg.trading, "live"):
                mode_cn = "实盘模式" if cfg.trading.live else "纸盘模式"
            if hasattr(cfg, "risk_limits"):
                shadow = bool(cfg.risk_limits.shadow_mode or False)
                max_eq = float(cfg.risk_limits.live_max_equity_usdt or max_eq)
                max_daily_loss = float(cfg.risk_limits.live_max_daily_loss_usdt or max_daily_loss)
        if shadow:
            mode_cn = f"{mode_cn}(影子 SHADOW)"
        state_snapshot: dict[str, Any] = {}
        started_at: int | None = None
        if store is not None and hasattr(store, "load"):
            try:
                state_snapshot = store.load() or {}
                raw = state_snapshot.get("started_at")
                if isinstance(raw, int) and raw > 0:
                    started_at = raw
                elif isinstance(raw, float) and raw > 0:
                    started_at = int(raw)
                elif isinstance(raw, str):
                    # 兼容老格式字符串迁移（新代码写入都为 int epoch；仅老数据兜底）
                    try:
                        import datetime as _dt_m
                        started_at = int(_dt_m.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp())
                    except Exception:  # noqa: BLE001
                        started_at = None
            except Exception:
                state_snapshot = {}
        # ----------------------------------------------------------------
        # 2026-08-31 修复 Bug A：started_at 仍 None（典型：recoverer 异常路径 / 旧 config 手改 / PID 新进程没跑到 run.py 兜底）
        # 这里是 Dashboard / 诊断 最后一道防线——再空就写当前 time.time() 回 StateStore，
        # 保证 /api/diag 和 Dashboard「启动时间/运行时长」至少是个合理值。
        # ----------------------------------------------------------------
        if not (isinstance(started_at, int) and started_at > 0):
            _fallback_epoch = int(_t.time())
            started_at = _fallback_epoch
            if store is not None and hasattr(store, "save"):
                try:
                    if not isinstance(state_snapshot, dict):
                        state_snapshot = {}
                    state_snapshot["started_at"] = _fallback_epoch
                    store.save(state_snapshot)
                except Exception:  # noqa: BLE001
                    pass
        # uptime: started_at 有值 = now - started_at；否则 None（保持接口向后兼容）
        uptime_seconds = int(_t.time() - started_at) if isinstance(started_at, int) and started_at > 0 else None
        # 防御：时区时钟漂移 / 测试虚构未来时间 时避免负数显示（避免 -17000s 这种荒谬值）
        if isinstance(uptime_seconds, int) and uptime_seconds < 0:
            uptime_seconds = 0
        # 人类可读本地时间（便于 Dashboard / 排查直接看）
        started_at_local = None
        if isinstance(started_at, int) and started_at > 0:
            try:
                started_at_local = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(started_at))
            except Exception:  # noqa: BLE001
                started_at_local = None
        # uptime 人类可读：XhYmZs
        uptime_human = None
        if isinstance(uptime_seconds, int) and uptime_seconds >= 0:
            h, rem = divmod(uptime_seconds, 3600)
            m, s = divmod(rem, 60)
            if h > 0:
                uptime_human = f"{h}h{m:02d}m{s:02d}s"
            elif m > 0:
                uptime_human = f"{m}m{s:02d}s"
            else:
                uptime_human = f"{s}s"

        system_block: dict[str, Any] = {
            "runtime_mode": mode_cn,
            "started_at": started_at,
            "started_at_local": started_at_local,
            "uptime_seconds": uptime_seconds,
            "uptime_human": uptime_human,
            "pid": _os.getpid(),
            "python_version": f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}",
            "version": version,
            "cwd": str(_P.cwd()),
            "platform": _sys.platform,
            "live_max_equity_usdt": max_eq,
            "live_max_daily_loss_usdt": max_daily_loss,
        }
        # 2026-08-30: 不开仓可观测：把最近风控结论/建议名义/缺口/双过时间戳写入 system（curl 粘贴直接可读）
        status_from_ctl: dict[str, Any] | None = None
        if ctl is not None and hasattr(ctl, "get_status_dict"):
            try:
                status_from_ctl = ctl.get_status_dict() or None
            except Exception:  # noqa: BLE001
                status_from_ctl = None
        if isinstance(status_from_ctl, dict):
            rsk_ctl = status_from_ctl.get("风控状态") or {}
            if isinstance(rsk_ctl, dict):
                last_r = rsk_ctl.get("最近一次风控") or {}
                if isinstance(last_r, dict):
                    system_block["last_risk_conclusion"] = last_r.get("结论")
                    system_block["last_risk_reason"] = (str(last_r.get("原因") or "")[:200] or None)
                    system_block["last_risk_suggested_notional_usdt"] = last_r.get("建议名义价值(USDT)")
                    system_block["last_risk_min_notional_usdt"] = last_r.get("最小名义(USDT)")
                    system_block["last_risk_capital_gap_usdt"] = last_r.get("缺口本金(USDT)")
                    system_block["last_risk_suggested_leverage"] = last_r.get("建议杠杆(X)")
                system_block["last_risk_evaluated_at"] = (
                    int(last_r.get("时间戳")) if isinstance(last_r, dict) and isinstance(last_r.get("时间戳"), (int, float)) else None
                )
                pt_ts = int(rsk_ctl.get("最近一次交易信号就绪时间戳") or 0)
                if pt_ts > 0:
                    system_block["last_trade_signal_ready_at"] = pt_ts
                    age = max(0, int(_t.time()) - pt_ts)
                    hh, rem = divmod(age, 3600); mm, ss = divmod(rem, 60)
                    system_block["last_trade_signal_ready_age"] = (
                        f"{hh}h{mm:02d}m{ss:02d}s" if hh > 0
                        else (f"{mm}m{ss:02d}s" if mm > 0 else f"{ss}s")
                    )
                else:
                    system_block["last_trade_signal_ready_at"] = None
                    system_block["last_trade_signal_ready_age"] = None
                ai_b = status_from_ctl.get("最近AI判断") or {}
                if isinstance(ai_b, dict):
                    regime = ai_b.get("市场状态")
                    conf = int(ai_b.get("置信度") or 0)
                    signal_ok = (isinstance(regime, str) and regime in ("上涨趋势", "下跌趋势") and conf >= 50)
                    system_block["ai_signal_status"] = (
                        f"到位[{regime} conf={conf}]" if signal_ok else f"不足[{regime or '暂无'} conf={conf}]"
                    )
                    # 2026-08-31 修复：先判「当前是否真的空仓」，再决定是否输出 why_no_position
                    #   (broker_block 还没 build，提前读一次 broker position 判断 has_pos)
                    pos_snapshot = {"side": "FLAT", "size": 0.0, "entry_price": 0.0, "mark_price": 0.0,
                                    "leverage": 1, "unrealized_pnl": 0.0}
                    if ctl is not None and hasattr(ctl, "broker"):
                        try:
                            sym_check = (getattr(getattr(cfg, "trading", None), "symbol", None) if cfg else None) or "ETH-USDT-SWAP"
                            p_tmp = await ctl.broker.get_position(sym_check)
                            pos_snapshot = {
                                "side": str(p_tmp.side.value) if hasattr(p_tmp.side, "value") else str(p_tmp.side),
                                "size": float(p_tmp.size or 0.0),
                                "entry_price": float(p_tmp.entry_price or 0.0),
                                "mark_price": float(p_tmp.mark_price or 0.0),
                                "leverage": int(getattr(p_tmp, "leverage", 1) or 1),
                                "unrealized_pnl": float(getattr(p_tmp, "unrealized_pnl", 0.0) or 0.0),
                            }
                        except Exception:  # noqa: BLE001
                            pass
                    has_pos_now = pos_snapshot["side"] not in ("FLAT", None) and pos_snapshot["size"] > 0
                    # 已持仓：why_no_position 直接显示「现有持仓概况」，不再误报「信号已发仍未持仓」
                    if has_pos_now:
                        side_cn = "多单(LONG)" if pos_snapshot["side"] in ("LONG",) else (
                            "空单(SHORT)" if pos_snapshot["side"] in ("SHORT",) else str(pos_snapshot["side"])
                        )
                        upl_sgn = f"+{pos_snapshot['unrealized_pnl']:.4f}U" if pos_snapshot["unrealized_pnl"] >= 0 else f"{pos_snapshot['unrealized_pnl']:.4f}U"
                        system_block["why_no_position"] = (
                            f"✅ 已持仓 {pos_snapshot['size']:.1f} 张({side_cn})，成本 {pos_snapshot['entry_price']:.1f}$ / "
                            f"现价 {pos_snapshot['mark_price']:.1f}$ / {pos_snapshot['leverage']}X / 浮盈亏 {upl_sgn}"
                        )
                    elif isinstance(last_r, dict) and last_r.get("结论") == "通过" and signal_ok:
                        system_block["why_no_position"] = (
                            "风控+AI双过，信号应已发送；仍未持仓 → 查日志 grep '[主循环]' / '[开仓]' / 'SHADOW' / execute 结果"
                        )
                    elif isinstance(last_r, dict) and last_r.get("结论") == "拒绝":
                        system_block["why_no_position"] = "风控拒绝：" + (str(last_r.get("原因") or "")[:200])
                    elif isinstance(last_r, dict) and last_r.get("结论") == "通过":
                        system_block["why_no_position"] = f"风控通过(名义 {last_r.get('建议名义价值(USDT)')}U ≥ min {last_r.get('最小名义(USDT)')}U @ {last_r.get('建议杠杆(X)')}X)，等 AI信号到位或成交"
                    elif last_r.get("结论") == "未执行" if isinstance(last_r, dict) else True:
                        system_block["why_no_position"] = "风控未执行：等下一轮 bg_main_loop 10s"


        # ── 2) broker ──────────────────────────────────────────────
        broker_block: dict[str, Any] = {"available": False, "broker_type": None}
        bal_total = None; bal_avail = None; bal_upl = None
        if ctl is not None and hasattr(ctl, "broker"):
            broker = ctl.broker
            bname = broker.__class__.__name__
            broker_block["broker_type"] = bname
            # Bugfix(用户 payload): 纸盘模式下如果误注入了 OKXBroker（实盘类）会触发外网
            # RequestTimeout。这里根据 broker 类名做短路：PaperBroker 承诺纯本地，直接
            # available=True，不做任何网络 IO。
            is_paper = (bname == "PaperBroker")
            sym = getattr(cfg.trading, "symbol", None) or "ETH-USDT-SWAP" if cfg else "ETH-USDT-SWAP"
            try:
                bal = await broker.get_balance()
                bal_total = float(getattr(bal, "total", 0.0) or 0.0)
                bal_avail = float(getattr(bal, "available", 0.0) or 0.0)
                bal_upl = float(getattr(bal, "unrealized_pnl", 0.0) or 0.0)
                broker_block.update({
                    "available": True,
                    "balance_total": bal_total,
                    "balance_available": bal_avail,
                    "balance_unrealized_pnl": bal_upl,
                })
                pos = await broker.get_position(sym)
                broker_block["position"] = {
                    "symbol": pos.symbol,
                    "side": pos.side.value,
                    "size": float(pos.size),
                    "entry_price": float(pos.entry_price),
                    "mark_price": float(pos.mark_price),
                    "unrealized_pnl": float(pos.unrealized_pnl),
                    "leverage": int(pos.leverage),
                    "liquidation_price": float(pos.liquidation_price or 0.0),
                }
                opens = await broker.get_open_orders(sym)
                broker_block["open_order_count"] = len(opens or [])
                if is_paper and "position" not in broker_block:
                    # PaperBroker 正常应已填充；这里兜底补 paper_only=true 标识
                    broker_block["paper_only"] = True
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                broker_block["error"] = err
                # 如果是 PaperBroker 但居然抛异常，也算致命；如果是 live=false 但 broker=OKXBroker
                #（工厂串台），给一条明确的修复建议，别只显示 RequestTimeout 让用户摸不着头
                live_flag = bool(getattr(getattr(cfg, "trading", None), "live", False)) if cfg else False
                if bname == "OKXBroker" and not live_flag:
                    broker_block["hint"] = (
                        "【异常】纸盘模式(live=false) 但 broker=OKXBroker（实盘类），触发外网 OKX 请求。"
                        "请检查：① app/broker/factory.py build_broker 是否正确使用 mode；"
                        "② run.py 是否正确调用了 build_broker(cfg)；"
                        "③ Windows 下若无代理/直连不通，必须走 PaperBroker（纯本地）。"
                    )
                elif is_paper:
                    broker_block["hint"] = "PaperBroker 本地 IO 失败，排查 data/ 目录权限"
                else:
                    broker_block["hint"] = (
                        "实盘 OKX 请求超时：检查 ① 代理(127.0.0.1:10808) 或直连是否通畅；"
                        "② OKX 账户 IP 白名单；③ 密钥 okx.api_key/secret/passphrase 正确。"
                    )

        # ── 3) controller / AI ────────────────────────────────────
        controller_block: dict[str, Any] = {"controller_available": False}
        if ctl is not None:
            try:
                ai_ts = None; ai_regime = None; ai_conf = 0; ai_reason = ""
                last_ai = getattr(ctl, "_last_ai", None)
                last_ai_ts = getattr(ctl, "_last_ai_ts", None)
                if last_ai is not None:
                    ai_regime = getattr(last_ai.market_regime, "value", str(last_ai.market_regime)) if hasattr(last_ai.market_regime, "value") else str(last_ai.market_regime)
                    ai_conf = int(getattr(last_ai, "confidence", 0) or 0)
                    ai_reason = str(getattr(last_ai, "reason", "") or "")
                    ai_ts = last_ai_ts
                # 2026-08-30：额外输出 daily_start 合理性指标（用户 VPS 遇到 1000U vs 14.83U 假熔断 → 一行看懂）
                risk_obj = getattr(ctl, "risk", None)
                ds = float(getattr(risk_obj, "daily_start_balance", 0.0) or 0.0)
                cur_equity = float(bal_total or 0.0)
                ds_ratio = (ds / cur_equity) if (ds > 0 and cur_equity > 0) else 0.0
                ds_diff_usdt = (ds - cur_equity) if (ds > 0 and cur_equity > 0) else 0.0
                # daily_loss_pct 直接用 RiskEngine 同款公式算一份给 /api/diag，避免前端再推断
                ds_pct = (1 - cur_equity / ds) * 100 if (ds > 1e-9 and cur_equity > 0) else 0.0
                daily_reset_day = (state_snapshot or {}).get("daily_reset_day") or ""
                import datetime as _dt
                _today = _dt.date.today().isoformat()
                ds_sanity = "正常"
                if ds <= 0:
                    ds_sanity = "未初始化(下一轮补)"
                elif cur_equity <= 0:
                    ds_sanity = "当前权益不可读(等 OKX)"
                elif ds_ratio >= 3.0:
                    ds_sanity = f"⚠️ 异常偏大（ds/cur={ds_ratio:.2f}x），建议重启或等下一轮 apply_daily_reset 纠偏"
                elif ds_diff_usdt < -50 and cur_equity > ds * 2.0:
                    ds_sanity = "ℹ️ 异常偏小（疑似充值未重启），盈亏率展示会偏大"
                elif abs(ds_diff_usdt) <= 50 and 0.7 <= ds_ratio <= 1.43:
                    ds_sanity = "正常（日内盈亏范围内）"
                controller_block = {
                    "controller_available": True,
                    "last_ai": {
                        "ts_ms": ai_ts,
                        "regime": ai_regime,
                        "confidence": ai_conf,
                        "reason_preview": ai_reason[:120],
                    },
                    "risk": {
                        "consecutive_losses": int(getattr(risk_obj, "consecutive_losses", 0) or 0),
                        "cooldown_until_ts": int(getattr(risk_obj, "cooldown_until_ts", 0) or 0),
                        "daily_start_balance": ds,
                        # ---- 2026-08-30 新增：可观测性 ----
                        "daily_reset_day": daily_reset_day,   # 上次写进 state.json 的日期
                        "today": _today,
                        "daily_loss_pct_if_trust": round(ds_pct, 4),  # ≈ RiskEngine 同款公式
                        "daily_start_vs_cur_ratio": round(ds_ratio, 4),
                        "daily_start_minus_cur_usdt": round(ds_diff_usdt, 4),
                        "sanity_status": ds_sanity,
                        "sanity_log": str(getattr(risk_obj, "daily_start_sanity_log", "") or ""),
                    },
                }
            except Exception as e:  # noqa: BLE001
                controller_block["error"] = f"{type(e).__name__}: {e}"

        # ── 4) position_manager（利润阶梯 / 累计盈亏） ──────────
        pm_block: dict[str, Any] = {}
        pm_saved = (state_snapshot or {}).get("position_manager") or {}
        if ctl is not None and hasattr(ctl, "risk"):
            pm_block = {
                "current_lock_pct": float(pm_saved.get("current_lock_pct", 0.0) or 0.0),
                "trailing_stop_price": float(pm_saved.get("trailing_stop_price", 0.0) or 0.0),
                "ladder_trigger_count": int(pm_saved.get("ladder_trigger_count", 0) or 0),
                "realized_pnl_usdt": float(pm_saved.get("realized_pnl_usdt", 0.0) or 0.0),
                "unrealized_pnl_usdt": bal_upl if (bal_upl is not None) else float(
                    pm_saved.get("unrealized_pnl_usdt", 0.0) or 0.0
                ),
                "peak_equity_usdt": float(pm_saved.get("peak_equity_usdt", bal_total or 0.0) or bal_total or 0.0),
                "drawdown_from_peak_pct": 0.0,
            }
            peak = float(pm_block["peak_equity_usdt"] or 0.0)
            cur = float(bal_total or (state_snapshot.get("balance") or {}).get("total", 0.0) or 0.0)
            if peak > 0 and cur > 0 and cur < peak:
                pm_block["drawdown_from_peak_pct"] = round((1 - cur / peak) * 100, 3)

        # ── 5) journal（近 24h 统计） ───────────────────────────
        journal_block: dict[str, Any] = {"journal_available": False}
        stats = (state_snapshot or {}).get("stats") or {}
        if ctl is not None and hasattr(ctl, "journal"):
            try:
                total_records = 0
                recent50 = []
                if hasattr(ctl.journal, "read_all"):
                    records = list(ctl.journal.read_all() or [])
                    total_records = len(records)
                    recent50 = records[-50:]
                # Top 失败原因
                failure_reasons: dict[str, int] = {}
                for r in recent50 or []:
                    result = str(getattr(r, "result", "") or r.get("结果") or "")
                    if "亏损" in result or "止损" in result or "STOP" in result:
                        extra = str(getattr(r, "extra", "") or r.get("附加信息") or "")
                        key = extra[:30] if extra else result[:30]
                        failure_reasons[key] = failure_reasons.get(key, 0) + 1
                top_failures = sorted(failure_reasons.items(), key=lambda x: x[1], reverse=True)[:3]
                journal_block = {
                    "journal_available": True,
                    "total_records": total_records,
                    "trades_closed": int(stats.get("trades_closed", stats.get("wins", 0) + stats.get("losses", 0)) or 0),
                    "wins": int(stats.get("wins", 0) or 0),
                    "losses": int(stats.get("losses", 0) or 0),
                    "total_pnl_pct": round(float(stats.get("total_pnl_pct", 0.0) or 0.0), 3),
                    "top_3_failure_reasons": [{"reason": k, "count": v} for k, v in top_failures],
                }
            except Exception as e:  # noqa: BLE001
                journal_block["error"] = f"{type(e).__name__}: {e}"

        # ── 6) safety ─────────────────────────────────────────────
        from app.core.safety import (  # noqa: PLC0415
            check_emergency_halt_file, detect_risks, _is_placeholder,
        )
        # project_root 已在入口处赋值模块级 PROJECT_ROOT（跨平台一致）；此处仅引用
        halt_exists, halt_reason = check_emergency_halt_file(project_root / "data" / "EMERGENCY_HALT")
        placeholder_okx = 0; placeholder_ai = 0
        if cfg is not None:
            placeholder_okx = sum(1 for x in (cfg.okx.api_key, cfg.okx.secret, cfg.okx.passphrase) if _is_placeholder(x))
            placeholder_ai = 1 if _is_placeholder(cfg.ai.api_key) else 0
        safety_block: dict[str, Any] = {
            "emergency_halt_exists": halt_exists,
            "emergency_halt_reason": halt_reason,
            "okx_placeholder_key_count": placeholder_okx,
            "ai_placeholder_key_count": placeholder_ai,
            "runtime_config_loaded": cfg is not None,
            "shadow_mode": shadow,
        }

        # ── 7) offline / fixtures：已按用户要求移除 ──────────────
        #    2026-08-29 用户明确「历史数据+pytest 多余，抓紧上实盘」，因此本段仅保留
        #    顶层键 fixtures（以防老客户端/断言直接读取 body["fixtures"]），但所有
        #    与 fixtures 模块 / 18×CSV.GZ 文件相关的字段、classify 逻辑、stage8/9
        #    pytest 子进程、?fixture= URL 参数全删。
        #    · file_count / present / sources：改为"removed"，不再维持 18 逻辑槽位
        #      （因为用户明确问「fixtures 有什么用 可以去掉吗」，回答里解释完毕，
        #      代码里就不必留伪 18 信号造成误解）。
        fixtures_block: dict[str, Any] = {
            "status": "removed_by_user_request_2026-08-29",
            "note": (
                "离线 K 线 fixtures 已完全移除（tests/fixtures/、app/storage/fixtures.py、"
                "deploy/pull_real_okx_klines.py、/api/ai/analyze?fixture= 参数均已删除）。"
                "实盘不再依赖任何离线 CSV；AI 分析直接走 controller.analyze() 的实时行情路径。"
            ),
            # 以下字段仅为兼容旧客户端做"最小占位"，数值无意义，不要再读它们。
            "file_count": None,
            "present_on_disk": None,
            "sources": None,
            "hint": None,
            "stage9_no_backup_pass": None,
            "stage9_info": None,
            "stage8_thresholds_pass": None,
            "stage8_info": None,
        }

        # ── 8) risks：自动缺陷检测 Top N ─────────────────────────
        try:
            risks = detect_risks(cfg)
        except Exception as e:  # noqa: BLE001
            risks = [f"[ERROR] detect_risks 异常：{type(e).__name__}: {e}"]
        # 再拼几个「实际运行时」的动态风险（比静态 config 检测更贴近真运行）
        if bal_total is not None and bal_total > max_eq:
            risks.append(f"[WARN] 真实账户总权益 {bal_total:.2f} U > 本金上限硬锁 {max_eq:.2f} U，超过部分未被保护（A1 护栏仅逻辑拦截，建议子账户严格限额）。")
        if bal_total is not None and bal_total < 10 and mode_cn.startswith("实盘") and not shadow:
            risks.append(f"[INFO] 账户极小（{bal_total:.2f} U）+ 已进实盘无影子：注意 ETH-USDT-SWAP OKX 最小下单额约 1 U，太小会被交易所直接拒单。")
        if halt_exists:
            risks.append(f"[FATAL] EMERGENCY_HALT 文件仍存在：{halt_reason} → 请处理完后手动删除，否则系统永不恢复开仓。")

        return {
            "generated_at_ms": int(_t.time() * 1000),
            "system": system_block,
            "broker": broker_block,
            "controller": controller_block,
            "position_manager": pm_block,
            "journal": journal_block,
            "safety": safety_block,
            "fixtures": fixtures_block,
            "risks": list(risks)[:5],
        }

    return app
