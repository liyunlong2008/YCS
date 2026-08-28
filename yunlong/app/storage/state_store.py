# -*- coding: utf-8 -*-
"""state.json 存储：账户 / 持仓 / 系统状态（设计文档 · 第十五节）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


class StateStore:
    """简单 JSON 状态持久化，带原子写。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "state.json"

    # ------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取 state.json；不存在则返回默认初态。"""
        if not self._path.exists():
            return self._default()
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("state.json 损坏，返回默认初态")
            return self._default()

    # ------------------------------------------------------------------
    def save(self, state: dict[str, Any]) -> None:
        """原子写入 state.json（先写 tmp 再 replace）。"""
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "status": "STOPPED",          # 参考 SystemStatus
            "started_at": None,
            "balance": {"total": 0.0, "available": 0.0, "unrealized_pnl": 0.0},
            "position": None,             # None 表示空仓
            "last_ai": None,
            "risk": {
                "consecutive_losses": 0,
                "cooldown_until_ts": 0,
                "daily_start_balance": 0.0,
            },
            "stats": {
                "trades_total": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl_pct": 0.0,
            },
        }
