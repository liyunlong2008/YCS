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

from fastapi import FastAPI, HTTPException, Query, Request
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

    def _collect_dashboard_data(rt: dict[str, Any]) -> dict[str, Any]:
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

        # 5) 持仓：从 state_store.position 或实时 balance+position 推断；Controller 可用时实时刷新
        pos_side = "空仓"
        pos_size = 0.0; pos_entry = 0.0; pos_mark = 0.0; pos_upl = 0.0
        pos_saved = snapshot.get("position") or {}
        if pos_saved:
            pos_side = _zh(pos_saved.get("side") or "FLAT")
            pos_side = "空仓" if pos_side in ("—", "FLAT") else pos_side
            pos_size = float(pos_saved.get("size", 0.0) or 0.0)
            pos_entry = float(pos_saved.get("entry_price", 0.0) or 0.0)
            pos_mark = float(pos_saved.get("mark_price", 0.0) or 0.0)
            pos_upl = float(pos_saved.get("unrealized_pnl", 0.0) or 0.0)
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
            "trades": trades_rows,
        }

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, summary="仪表盘首页")
    async def index(request: Request) -> str:
        """中文仪表盘首页（Dashboard 全部使用中文）—— 服务端直出骨架，JS 刷新。"""
        import json as _json
        d = _collect_dashboard_data(request.app.state.runtime)
        tag_class = "live" if d["mode"] == "实盘模式" else ""
        mode_tag = f'<span class="tag {tag_class}">{d["mode"]}</span>'
        # 2026-08-30 新增：AI 节流 7 级彩色 tag（紧邻运行模式）
        thr_tag = (
            f'<span id="k-thr-tag" class="tag" style="{d["thr_color"]}">'
            f'AI 节流 · {d["thr_level"]}</span>'
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
          <h1>云龙挑战赛 · Dashboard {mode_tag}{thr_tag}<span id="k-updated" class="updated"></span></h1>

          <div class="grid">
            <!-- 运行模式 -->
            <div class="card">
              <h2>运 行 模 式</h2>
              <div class="kv">
                <div class="label">运行模式</div><div id="k-mode" class="value">{d['mode']}</div>
                <div class="label">系统状态</div><div id="k-status" class="value">{d['status']}</div>
                <div class="label">累计收益率 (%)</div><div id="k-total-pnl" class="value {'loss' if d['total_pnl_pct']<0 else 'win'}">{d['total_pnl_pct']:.2f}%</div>
                <div class="label">已平仓交易</div><div id="k-closed" class="value">{d['closed']} 笔（胜{d['wins']}/败{d['losses']}）</div>
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

                // AI 节流（2026-08-30 新增：顶部彩色 tag + 卡片）
                const thr = s['AI节流状态'] || {{}};
                const thrLevel = String(thr['节流级别'] ?? 'NORMAL');
                const thrColor = String(thr['节流颜色'] ?? '');
                const elTag = document.getElementById('k-thr-tag');
                if (elTag) {{
                  elTag.textContent = 'AI 节流 · ' + thrLevel;
                  if (thrColor) elTag.setAttribute('style', thrColor);
                }}
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
            "启动时间": None,
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
            "风控状态": {
                "连续亏损次数": 0,
                "熔断冷却至(秒时间戳)": 0,
                "是否允许开仓": "否",
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
    async def api_kill(request: Request):
        """三通道之一：HTTP POST。其它通道：ycsctl kill、data/EMERGENCY_HALT 文件。
           安全：必须带 X-YCS-Admin-Token 头 == config.risk_limits.kill_switch_token。
        """
        from fastapi import status as _st
        rt = request.app.state.runtime or {}
        cfg = rt.get("config")
        expected_token = ""
        if cfg is not None and hasattr(cfg, "risk_limits"):
            expected_token = str(getattr(cfg.risk_limits, "kill_switch_token", "") or "")
        got_token = str(request.headers.get("x-ycs-admin-token") or "")
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
        started_at = None
        if store is not None and hasattr(store, "load"):
            try:
                state_snapshot = store.load() or {}
                started_at = state_snapshot.get("started_at")
            except Exception:
                state_snapshot = {}
        system_block: dict[str, Any] = {
            "runtime_mode": mode_cn,
            "started_at": started_at,
            "uptime_seconds": int(_t.time() - started_at) if isinstance(started_at, (int, float)) and started_at > 0 else None,
            "pid": _os.getpid(),
            "python_version": f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}",
            "version": version,
            "cwd": str(_P.cwd()),
            "platform": _sys.platform,
            "live_max_equity_usdt": max_eq,
            "live_max_daily_loss_usdt": max_daily_loss,
        }

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
                controller_block = {
                    "controller_available": True,
                    "last_ai": {
                        "ts_ms": ai_ts,
                        "regime": ai_regime,
                        "confidence": ai_conf,
                        "reason_preview": ai_reason[:120],
                    },
                    "risk": {
                        "consecutive_losses": int(getattr(getattr(ctl, "risk", None), "consecutive_losses", 0) or 0),
                        "cooldown_until_ts": int(getattr(getattr(ctl, "risk", None), "cooldown_until_ts", 0) or 0),
                        "daily_start_balance": float(getattr(getattr(ctl, "risk", None), "daily_start_balance", 0.0) or 0.0),
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
