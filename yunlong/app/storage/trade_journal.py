# -*- coding: utf-8 -*-
"""Trade Journal：JSONL 追加写入（设计文档 · 第十六节）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..core.constants import MarketRegime


class TradeRecord(BaseModel):
    """单笔交易决策 / 结果记录。"""
    time: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    market_regime: MarketRegime | str = MarketRegime.LOW_VOLATILITY
    confidence: int = 0
    entry_reason: str = ""
    result: str = ""    # 如 +2.5R / -1R / +3.1%
    extra: dict = Field(default_factory=dict)


class TradeJournal:
    """JSONL 追加式交易日志。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "trades.jsonl"

    def append(self, rec: TradeRecord) -> None:
        """追加一条交易记录。"""
        with self._path.open("a", encoding="utf-8") as f:
            f.write(rec.model_dump_json(ensure_ascii=False) + "\n")

    def read_all(self) -> list[TradeRecord]:
        """读取所有历史记录。"""
        if not self._path.exists():
            return []
        out: list[TradeRecord] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(TradeRecord.model_validate_json(line))
                except Exception:
                    # 跳过损坏行
                    pass
        return out
