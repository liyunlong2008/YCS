# -*- coding: utf-8 -*-
"""FastAPI Dashboard 应用工厂。

Dashboard 全部使用中文（设计文档 · 第二十节）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ..core.constants import RunMode

# 中文映射表（设计文档 · 第二十节）
ZH_STATUS = {
    "RUNNING": "运行中",
    "STOPPED": "已停止",
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
    """构建 FastAPI 应用。阶段 3 补齐完整 UI / 接口。"""
    app = FastAPI(
        title="云龙挑战赛 Dashboard",
        version="1.0.0",
        docs_url="/docs",
    )

    # 全局上下文：运行时由 run.py / 服务层注入。
    # 此处仅保留占位，避免 Dashboard 启动即依赖 OKX / AI。
    app.state.runtime: dict[str, Any] = {
        "config": None,
        "broker": None,
        "ai": None,
        "risk": None,
        "storage": None,
    }

    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, summary="仪表盘首页")
    async def index() -> str:
        """中文仪表盘首页（阶段 3 完善 UI）。"""
        st = app.state.runtime
        mode = RunMode.LIVE if st["config"] and st["config"].trading.live else RunMode.PAPER
        return f"""
        <html lang="zh-CN">
        <head><meta charset="UTF-8"><title>云龙挑战赛 Dashboard</title></head>
        <body style="font-family:-apple-system,'PingFang SC',sans-serif;padding:32px;">
          <h1>云龙挑战赛 Dashboard</h1>
          <hr/>
          <ul>
            <li>运行模式：{_zh(mode.value)}</li>
            <li>系统状态：{_zh(None)}</li>
            <li>账户余额：0.00 USDT</li>
            <li>当前持仓：空仓</li>
            <li>累计收益：0.00%</li>
          </ul>
          <p>本 Dashboard 采用中文界面（设计文档 · 第二十节）。</p>
        </body>
        </html>
        """

    @app.get("/api/health", summary="健康检查")
    async def health() -> dict:
        return {"ok": True, "message": "云龙挑战赛系统运行中"}

    return app
