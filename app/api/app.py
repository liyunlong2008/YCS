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


def create_app(
    config_path: Path | str | None = None,
    *,
    runtime: dict[str, Any] | None = None,
    on_startup: list[Callable[[], Any]] | None = None,
    on_shutdown: list[Callable[[], Any]] | None = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    Args:
        config_path: 配置文件路径（预留，单测可不传）
        runtime: 可选预填充 runtime 字典（单测 / 离线调用使用）
        on_startup: 可选启动期同步回调列表（lifespan 启动时调用）
        on_shutdown: 可选关闭期同步回调列表（lifespan 关闭时调用）
    """

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
        "config": None,
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
        """服务端聚合首页需要的字段；缺失时填默认值以保证 HTML 有骨架文本。"""
        # 1) 运行模式
        mode_cn = "纸盘模式"
        cfg = rt.get("config")
        if cfg is not None and hasattr(cfg, "trading"):
            try:
                from ..core.constants import RunMode
                mode_cn = "实盘模式" if getattr(cfg.trading, "live", False) else "纸盘模式"
            except Exception:
                mode_cn = "纸盘模式"

        # 2) state_store 余额/stats；若 Controller 可用则优先走 Controller
        ctl = rt.get("controller")
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

        total = float(bal.get("total", 0.0) or 0.0)
        available = float(bal.get("available", total) or total)
        upl = float(bal.get("unrealized_pnl", 0.0) or 0.0)
        daily_start = float(risk_dict.get("daily_start_balance", total or 1000.0) or 1000.0)
        total_pnl_pct = float(stats.get("total_pnl_pct", 0.0) or 0.0)

        wins = int(stats.get("wins", 0) or 0)
        losses = int(stats.get("losses", 0) or 0)
        closed = int(stats.get("trades_closed", wins + losses) or wins + losses)
        wr = (wins / closed * 100) if closed > 0 else 0.0

        # 3) 风控
        consec = int(risk_dict.get("consecutive_losses", 0) or 0)
        allow = bool(risk_dict.get("allow_trading", True))
        cooldown_until = risk_dict.get("cooldown_until") or 0
        cd_cn = "是" if allow else ("否（冷却至 " + (str(cooldown_until) if cooldown_until else "手动解除") + "）")
        daily_loss_pct = risk_dict.get("daily_loss_pct") or 0.0
        daily_status = "正常" if daily_loss_pct > -15.0 else f"已触发日亏限制（{daily_loss_pct:.2f}%）"

        # 4) 持仓（从 state_store.position 或 Controller.get_position_dict）
        pos_side = "空仓"
        pos_size = 0.0
        pos_entry = 0.0
        pos_mark = 0.0
        pos_upl = 0.0
        pos_saved = snapshot.get("position") or {}
        if pos_saved:
            pos_side = _zh(pos_saved.get("side") or "FLAT")
            pos_side = "空仓" if pos_side in ("—", "FLAT") else pos_side
            pos_size = float(pos_saved.get("size", 0.0) or 0.0)
            pos_entry = float(pos_saved.get("entry_price", 0.0) or 0.0)
            pos_mark = float(pos_saved.get("mark_price", 0.0) or 0.0)
            pos_upl = float(pos_saved.get("unrealized_pnl", 0.0) or 0.0)
        # 保护锁点（来自 position_manager）
        current_lock = float(pm_dict.get("current_lock_pct", 0.0) or 0.0)
        trailing = float(pm_dict.get("trailing_stop_price", 0.0) or 0.0)
        protect_txt = "未启用"
        if current_lock > 0:
            protect_txt = f"已锁 {current_lock:.1f}% 利润"
        elif trailing > 0:
            protect_txt = f"移动止损价 {trailing:.2f}"

        # 5) AI 上次判断（来自 state_store.last_ai）
        ai = snapshot.get("last_ai") or {}
        ai_regime = ai.get("market_regime") or "—"
        ai_conf = ai.get("confidence") or 0
        ai_direction = ai.get("suggested_direction") or "—"
        ai_reason = ai.get("reason_short") or "暂无"
        # regime 中文
        regime_map = {"TREND_UP": "上涨趋势", "TREND_DOWN": "下跌趋势", "RANGE": "震荡", "VOLATILE": "波动"}
        ai_regime_cn = regime_map.get(str(ai_regime).upper(), str(ai_regime))

        # 6) 最近交易（优先 journal；state_store 没 journal 就走空列表）
        trades_rows: list[dict] = []
        storage = rt.get("storage")
        if isinstance(storage, tuple) and len(storage) >= 2:
            journal = storage[1]
            if hasattr(journal, "read_recent"):
                try:
                    records = journal.read_recent(limit=20) or []
                    for r in records:
                        trades_rows.append({
                            "时间": r.get("timestamp") or r.get("时间") or "—",
                            "市场状态": regime_map.get(str(r.get("market_regime") or r.get("市场状态") or "—"),
                                                     str(r.get("market_regime") or r.get("市场状态") or "—")),
                            "置信度": r.get("confidence") or r.get("置信度") or 0,
                            "入场原因": r.get("reason_short") or r.get("入场原因") or "—",
                            "结果": r.get("result_r") or r.get("结果") or "—",
                        })
                except Exception:
                    trades_rows = []

        return {
            "mode": mode_cn,
            "status": snapshot.get("status") or "运行中",
            "bal_total": total, "bal_available": available, "bal_upl": upl,
            "daily_start": daily_start,
            "total_pnl_pct": total_pnl_pct,
            "wins": wins, "losses": losses, "closed": closed, "wr": wr,
            "consec": consec, "allow": cd_cn, "daily_status": daily_status,
            "daily_loss_pct": daily_loss_pct,
            "pos_side": pos_side, "pos_size": pos_size, "pos_entry": pos_entry,
            "pos_mark": pos_mark, "pos_upl": pos_upl,
            "protect_txt": protect_txt,
            "ai_regime": ai_regime_cn, "ai_conf": ai_conf,
            "ai_direction": ai_direction, "ai_reason": ai_reason,
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
            footer {{ color:#888; font-size:12px; margin-top:20px; }}
          </style>
        </head>
        <body>
          <h1>云龙挑战赛 · Dashboard {mode_tag}</h1>

          <div class="grid">
            <div class="card">
              <h2>运 行 模 式</h2>
              <div class="kv">
                <div class="label">运行模式</div><div class="value">{d['mode']}</div>
                <div class="label">系统状态</div><div class="value">{d['status']}</div>
                <div class="label">累计收益率 (%)</div><div class="value {'loss' if d['total_pnl_pct']<0 else 'win'}">{d['total_pnl_pct']:.2f}%</div>
                <div class="label">已平仓交易</div><div class="value">{d['closed']} 笔</div>
              </div>
            </div>

            <div class="card">
              <h2>余 额</h2>
              <div class="kv">
                <div class="label">总权益 (USDT)</div><div class="value">{d['bal_total']:.2f}</div>
                <div class="label">可用保证金</div><div class="value">{d['bal_available']:.2f}</div>
                <div class="label">未实现盈亏</div><div class="value {'loss' if d['bal_upl']<0 else 'win'}">{d['bal_upl']:.2f}</div>
                <div class="label">今日起始权益</div><div class="value">{d['daily_start']:.2f}</div>
              </div>
            </div>

            <div class="card">
              <h2>风 控</h2>
              <div class="kv">
                <div class="label">是否允许开仓</div><div class="value">{d['allow']}</div>
                <div class="label">连续亏损</div><div class="value">{d['consec']} 次</div>
                <div class="label">今日盈亏率</div><div class="value {'loss' if d['daily_loss_pct']<0 else ''}">{d['daily_loss_pct']:.2f}%</div>
                <div class="label">胜率</div><div class="value">{d['wr']:.0f}% ({d['wins']}胜/{d['losses']}败)</div>
              </div>
            </div>

            <div class="card">
              <h2>持 仓</h2>
              <div class="kv">
                <div class="label">持仓方向</div><div class="value">{d['pos_side']}</div>
                <div class="label">数量</div><div class="value">{d['pos_size']:.6f}</div>
                <div class="label">开仓均价 / 标记价</div><div class="value">{d['pos_entry']:.2f} / {d['pos_mark']:.2f}</div>
                <div class="label">浮动盈亏 / 保护</div><div class="value">{'+' if d['pos_upl']>=0 else ''}{d['pos_upl']:.2f} · {d['protect_txt']}</div>
              </div>
            </div>

            <div class="card">
              <h2>AI 判 断</h2>
              <div class="kv">
                <div class="label">市场状态</div><div class="value">{d['ai_regime']}</div>
                <div class="label">置信度</div><div class="value">{d['ai_conf']:.0f}%</div>
                <div class="label">建议方向</div><div class="value">{d['ai_direction']}</div>
                <div class="label">简短理由</div><div class="value" style="text-align:left;grid-column:1/-1;">{d['ai_reason']}</div>
              </div>
            </div>
          </div>

          <div class="card">
            <h2>最 近 交 易</h2>
            <table>
              <thead><tr>
                <th>时间</th><th>市场状态</th><th>置信度</th><th>入场原因</th><th>结果</th>
              </tr></thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>

          <footer>本 Dashboard 遵循设计文档 · 第二十节：界面与 API 全部为中文。接口文档：<a href="/docs">/docs</a></footer>

          <script>
            // 每 5s 刷新数据，走 /api/status 与 /api/trades；未初始化（仅首页静态）时不抛错
            window.__DASHBOARD__ = {{data: {data_json}}};
            async function refresh() {{
              try {{
                const resp = await fetch('/api/status');
                if (!resp.ok) return;
                const s = await resp.json();
                // 不覆盖，有值则更新对应 DOM（id 与 /api/status 中文键一一对应以兼容）
              }} catch (_) {{ /* 未注入 controller 时静默 */ }}
            }}
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
        ctl = _get_controller(request)
        return ctl.get_status_dict()

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

    @app.get("/api/ai/analyze", summary="触发一次 AI 市场分析")
    async def api_ai_analyze(
        request: Request,
        fixture: str | None = Query(
            default=None,
            description="可选：离线场景名称（trend_up/trend_down/range）→ 使用仓库内 fixtures，无需联网 OKX",
            pattern="^(trend_up|trend_down|range)$",
        ),
    ) -> dict:
        """支持两种模式：
          1) 不传 fixture → 调 TradingController.analyze()，使用实时行情或 Broker 快照
          2) 传 fixture=场景名 → 从离线 K 线 fixtures 加载 ccxt ohlcv 喂给 AIAnalyzer，输出中文判断
        """
        if fixture is None:
            ctl = _get_controller(request)
            return await ctl.analyze()

        # 离线模式：无需 controller，直接用 AI/Analyzer + fixtures
        from app.storage.fixtures import load_all_timeframes  # noqa: PLC0415
        from app.ai.base import MarketAnalysisResult, MarketData  # noqa: PLC0415
        from app.core.config import AIConfig  # noqa: PLC0415
        from app.ai.factory import build_ai_provider  # noqa: PLC0415
        from app.core.constants import MarketRegime  # noqa: PLC0415

        runtime = request.app.state.runtime or {}
        cfg_obj = runtime.get("config")
        ai_settings = getattr(cfg_obj, "ai", None) if cfg_obj else None
        if ai_settings is None:
            ai_settings = AIConfig(
                provider="deepseek", api_key="", model="deepseek-chat",
                base_url="", timeout=10, retries=1,
            )

        # 找 AI 实例（优先 runtime）或新建
        provider = runtime.get("ai")
        # AI 密钥占位判定（优先 runtime.config 已标记 → 实时判定兜底）
        ai_key_placeholder = bool(getattr(getattr(runtime.get("config"), "ai", None), "_placeholder_api_key", False))
        if not ai_key_placeholder:
            from app.core.safety import _is_placeholder as _p  # noqa: PLC0415
            ai_key_placeholder = _p((ai_settings.api_key or "").strip())
        if provider is None and not ai_key_placeholder:
            provider = build_ai_provider(ai_settings)

        klines_by_tf = load_all_timeframes(fixture)  # type: ignore[arg-type]
        # 用 1h 最末一根 K 线构造 MarketData（最新一根 close 为当前价）
        last_1h = klines_by_tf["1h"][-1]
        current = MarketData(
            symbol="ETH-USDT-SWAP",
            timestamp=int(last_1h[0]),
            open=float(last_1h[1]), high=float(last_1h[2]),
            low=float(last_1h[3]), close=float(last_1h[4]),
            volume=float(last_1h[5]),
        )
        got_exception: Exception | None = None
        if provider is not None and not ai_key_placeholder:
            try:
                result = await provider.analyze_market(current)
            except Exception as exc:
                got_exception = exc
                result = None
        else:
            result = None

        if result is None:
            # ① AI 密钥是占位值 ② LiteLLM 抛错 两种场景 → 走确定性离线回退
            closes_1d = [float(x[4]) for x in klines_by_tf["1d"]]
            c0, c1 = closes_1d[0], closes_1d[-1]
            pct = (c1 - c0) / max(c0, 1e-12)
            amp = (max(closes_1d) - min(closes_1d)) / max(c0, 1e-12)
            if pct >= 0.05:
                regime, conf = MarketRegime.TREND_UP, 72
                short = f"离线判定：1d 涨幅 {pct*100:.1f}%，趋势做多"
            elif pct <= -0.05:
                regime, conf = MarketRegime.TREND_DOWN, 70
                short = f"离线判定：1d 跌幅 {pct*100:.1f}%，趋势做空"
            else:
                regime, conf = MarketRegime.RANGE, 60
                short = f"离线判定：1d 振幅 {amp*100:.1f}%，震荡观望"
            if got_exception is not None:
                short += f"（AI 异常: {type(got_exception).__name__}）"
            elif ai_key_placeholder:
                short += "（AI 密钥占位，跳过联网调用）"
            result = MarketAnalysisResult(
                market_regime=regime, confidence=conf, reason=short,
            )

        # 建议方向 / 入场 / 止损 / 止盈（MarketAnalysisResult 现无字段就用规则计算给 Dashboard 展示）
        regime = result.market_regime
        px = float(current.close)
        if regime is MarketRegime.TREND_UP:
            direction = "LONG"; stop = px * 0.985; take = px * 1.06; entry = px
        elif regime is MarketRegime.TREND_DOWN:
            direction = "SHORT"; stop = px * 1.015; take = px * 0.94; entry = px
        else:
            direction = "FLAT"; stop = None; take = None; entry = None

        return {
            "模式": f"离线 fixtures [{fixture}]",
            "数据条数": {tf: len(v) for tf, v in klines_by_tf.items()},
            "市场状态": result.market_regime.value,
            "置信度(%)": int(result.confidence),
            "建议方向": direction,
            "入场目标价(USDT)": entry,
            "止损价(USDT)": stop,
            "目标止盈价(USDT)": take,
            "简短理由": (result.reason or "")[:80],
            "详细": result.reason,
        }

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

        # ── 7) fixtures：逐文件分类 18 个 CSV.GZ + stage9/stage8 快速自检 ───
        from app.storage.fixtures import (  # noqa: PLC0415
            DEFAULT_ROOT as _FIX_ROOT, classify_all_fixture_files,
        )
        try:
            all_files = sorted(_FIX_ROOT.glob("*.csv.gz")) if _FIX_ROOT.exists() else []
            # Bugfix: 之前只抽样 3 场景×1d → sources 总和=3，无法准确反映 18 个文件里真实/合成
            # 的比例（用户要求一定要真实 K，需要能一眼看出还剩多少旧数据）。
            sources = classify_all_fixture_files(_FIX_ROOT)
            fixtures_block: dict[str, Any] = {
                "file_count": len(all_files),
                "root_dir": str(_FIX_ROOT),
                "sources": sources,
                "sources_sum_equals_18": sum(int(v) for v in sources.values()) == 18,
            }
            # stage9 / stage8 快速自检（非阻塞，90s 兜底；优先 uv，找不到 uv 时 fallback python -m pytest）
            pytest_common_args = [
                "-q", "--no-header", "--tb=no", "--no-header", "-p", "no:cacheprovider",
                "--override-ini=cache_dir=/tmp/pytest_ycs_diag_cache",
            ]
            try:
                ok9, info9 = _diag_run_pytest(
                    ["tests/test_stage9_no_backup.py", *pytest_common_args],
                    project_root=project_root, timeout_seconds=90,
                )
                ok8, info8 = _diag_run_pytest(
                    ["tests/test_stage8_market_fixtures.py", *pytest_common_args],
                    project_root=project_root, timeout_seconds=120,
                )
            except Exception as _e:
                ok9, info9 = False, f"未执行: {type(_e).__name__}"
                ok8, info8 = False, f"未执行: {type(_e).__name__}"
            fixtures_block["stage9_no_backup_pass"] = bool(ok9)
            fixtures_block["stage9_info"] = info9
            fixtures_block["stage8_thresholds_pass"] = bool(ok8)
            fixtures_block["stage8_info"] = info8
        except Exception as e:  # noqa: BLE001
            fixtures_block = {
                "file_count": 0, "error": f"{type(e).__name__}: {e}",
                "sources": {}, "stage9_no_backup_pass": None,
                "stage8_thresholds_pass": None,
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
