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

from pathlib import Path
from typing import Any

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


def create_app(config_path: Path | str | None = None) -> FastAPI:
    """构建 FastAPI 应用。"""
    app = FastAPI(
        title="云龙挑战赛 Dashboard",
        version="1.0.0",
        description="云龙挑战赛（YCS）：ETH-USDT-SWAP 单品种 AI 分析 + Maker 优先 + 严格风控 + 自动恢复 + 利润保护 自动交易系统。",
        docs_url="/docs",
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
    }

    def _get_controller(request: Request):
        ctl = request.app.state.runtime.get("controller")
        if ctl is None:
            raise HTTPException(status_code=503, detail="控制器尚未初始化，请先在 run.py 中注入 TradingController")
        return ctl

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, summary="仪表盘首页")
    async def index(request: Request) -> str:
        """中文仪表盘首页（Dashboard 全部使用中文）。"""
        mode_str = "纸盘模式"
        controller = request.app.state.runtime.get("controller")
        if controller is not None:
            from ..core.constants import RunMode
            mode_str = "实盘模式" if controller.config.trading.mode == RunMode.LIVE else "纸盘模式"

        return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="UTF-8"/>
          <meta name="viewport" content="width=device-width,initial-scale=1"/>
          <title>云龙挑战赛 Dashboard</title>
          <style>
            body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
                   padding:32px; background:#f6f7fb; color:#222; }}
            .card {{ background:#fff; border-radius:12px; padding:20px 24px; box-shadow:0 2px 10px rgba(0,0,0,.04); margin-bottom:16px; }}
            h1 {{ margin:0 0 12px; }} h2 {{ font-size:16px; margin:0 0 12px; color:#555; }}
            ul {{ list-style:none; padding:0; margin:0; display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }}
            li {{ background:#fafbfc; border-radius:8px; padding:10px 14px; }}
            .label {{ color:#888; font-size:12px; display:block; margin-bottom:4px; }}
            .value {{ font-size:16px; font-weight:600; }}
            .tag {{ display:inline-block; padding:2px 10px; border-radius:999px;
                     background:#e6f4ea; color:#137333; font-size:12px; margin-left:8px; }}
            .tag.live {{ background:#fce8e6; color:#c5221f; }}
            table {{ width:100%; border-collapse:collapse; }}
            th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #eee; font-size:13px; }}
            th {{ color:#777; font-weight:500; background:#fafbfc; }}
            a {{ color:#1a73e8; }}
          </style>
        </head>
        <body>
          <h1>云龙挑战赛 Dashboard <span class="tag {"live" if mode_str=="实盘模式" else ""}">{mode_str}</span></h1>
          <div class="card">
            <h2>系统总览</h2>
            <ul id="status-list">
              <li><span class="label">系统状态</span><span class="value" id="s-status">加载中…</span></li>
              <li><span class="label">账户余额</span><span class="value" id="s-balance">—</span></li>
              <li><span class="label">当前持仓</span><span class="value" id="s-position">—</span></li>
              <li><span class="label">累计收益</span><span class="value" id="s-pnl">—</span></li>
            </ul>
          </div>
          <div class="card"><h2>最近交易流水</h2><table id="trades"><thead><tr>
            <th>时间</th><th>市场状态</th><th>置信度</th><th>入场原因</th><th>结果</th>
          </tr></thead><tbody></tbody></table></div>
          <script>
            async function load() {{
              try {{
                const s = await (await fetch('/api/status')).json();
                document.getElementById('s-status').textContent = s['系统状态'] || '—';
                document.getElementById('s-balance').textContent = (s['账户余额总权益'] ?? 0).toFixed(2) + ' USDT';
                const pos = await (await fetch('/api/position')).json();
                document.getElementById('s-position').textContent =
                  pos && pos['持仓方向'] !== '空仓' ? `${{pos['持仓方向']}} · ${{pos['开仓均价']}}` : '空仓';
                const pnl = s['累计收益率(%)'] ?? 0;
                document.getElementById('s-pnl').textContent = pnl + '%';
                const t = await (await fetch('/api/trades?limit=20')).json();
                const tb = document.querySelector('#trades tbody');
                if (t.length === 0) tb.innerHTML = '<tr><td colspan=5>暂无交易记录</td></tr>';
                else tb.innerHTML = t.map(r => `<tr>
                  <td>${{r['时间']}}</td><td>${{r['市场状态']}}</td>
                  <td>${{r['置信度']}}</td><td>${{r['入场原因']}}</td><td>${{r['结果']}}</td>
                </tr>`).join('');
              }} catch (e) {{
                document.getElementById('s-status').textContent = '加载失败：' + e.message;
              }}
            }}
            load(); setInterval(load, 5000);
          </script>
          <p style="color:#888;font-size:12px;">本 Dashboard 遵循设计文档 · 第二十节：全部界面与 API 输出为中文。接口文档：<a href="/docs">/docs</a></p>
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
    async def api_ai_analyze(request: Request) -> dict:
        ctl = _get_controller(request)
        import asyncio
        return await ctl.analyze()

    return app
