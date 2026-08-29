"""
app.storage.fixtures —— 离线 K 线 fixtures 加载器

ccxt ohlcv 兼容格式：[[timestamp_ms, open, high, low, close, volume], ...]
"""
from __future__ import annotations

import csv
import gzip
from pathlib import Path
from typing import Literal

SCENES_LITERAL = Literal["trend_up", "trend_down", "range"]
TIMEFRAMES_LITERAL = Literal["1m", "5m", "15m", "1h", "4h", "1d"]

# 仓库内默认 fixtures 根目录
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "market_data"


def load_fixture(
    scene: SCENES_LITERAL,
    timeframe: TIMEFRAMES_LITERAL,
    *,
    root: Path | None = None,
    limit: int | None = None,
) -> list[list]:
    """
    返回 ccxt 兼容 ohlcv 升序 list；末尾为最新 bar。
    """
    root = root or DEFAULT_ROOT
    if scene not in ("trend_up", "trend_down", "range"):
        raise ValueError(f"不支持场景: {scene}（可选 trend_up/trend_down/range）")
    if timeframe not in ("1m", "5m", "15m", "1h", "4h", "1d"):
        raise ValueError(f"不支持周期: {timeframe}")
    tf_safe = timeframe.replace("/", "_")
    p = Path(root) / f"{scene}__{tf_safe}.csv.gz"
    if not p.is_file():
        raise FileNotFoundError(
            f"fixture 文件不存在：{p}\n"
            "请先执行：uv run python deploy/fetch_market_fixtures.py"
        )

    out: list[list] = []
    with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            out.append([
                int(r["timestamp"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["volume"]),
            ])
    if limit:
        out = out[-int(limit):]
    return out


def load_all_timeframes(
    scene: SCENES_LITERAL,
    *,
    root: Path | None = None,
    timeframes: tuple[TIMEFRAMES_LITERAL, ...] = ("1m", "5m", "15m", "1h", "4h", "1d"),
) -> dict[TIMEFRAMES_LITERAL, list[list]]:
    return {tf: load_fixture(scene, tf, root=root) for tf in timeframes}
