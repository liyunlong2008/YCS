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

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient  # noqa: F401  —— 方便测试里直接 import

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

    return app
