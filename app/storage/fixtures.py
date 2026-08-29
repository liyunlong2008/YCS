"""
app.storage.fixtures —— 离线 K 线 fixtures 加载器

ccxt ohlcv 兼容格式：[[timestamp_ms, open, high, low, close, volume], ...]
"""
from __future__ import annotations

import csv
import gzip
from pathlib import Path
from typing import Iterable, Literal

SCENES_LITERAL = Literal["trend_up", "trend_down", "range"]
TIMEFRAMES_LITERAL = Literal["1m", "5m", "15m", "1h", "4h", "1d"]

# 仓库内默认 fixtures 根目录
DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "market_data"

# 合成锚点（与 deploy/fetch_market_fixtures.py 中 GBM 合成器一致）
_SYNTH_START_TS_MS = 1767225600_000  # 2026-01-01 00:00:00 UTC
_SYNTH_BASE_PRICE = 2000.0            # 合成首 bar open/close 围绕的锚
_SYNTH_START_OPEN_EPS = 0.0005        # 合成首 bar open 相对 BASE_PRICE 的扰动上限

# 判定「真实 OKX 历史」必需同时满足：
#   1) 首根时间戳早于今天 UTC 0 点（真实数据不可能用未来日期）
#   2) 首根 open 不落在 [BASE*(1-EPS), BASE*(1+EPS)] 且 timestamp≠SYNTH_START_TS
#   3) 末根时间戳晚于 2019-01-01（排除占位/测试占位小文件）
_EARLIEST_REAL_END_MS = 1546300800_000   # 2019-01-01 UTC


def detect_fixture_source(
    scene: SCENES_LITERAL,
    *,
    root: Path | None = None,
    timeframes: Iterable[TIMEFRAMES_LITERAL] = ("1d", "1h", "4h"),
) -> Literal["real_okx", "synthetic_gbm", "mixed", "missing"]:
    """判断当前 fixture 数据来源：real_okx / synthetic_gbm / mixed / missing。

    判定逻辑：
      - 任意目标文件缺失 → "missing"
      - 所有抽检文件都命中「合成指纹」（首根 ts=2026-01-01 00:00 / open 紧贴 2000 锚
        且末根 ts 晚于首根）→ "synthetic_gbm"
      - 所有抽检文件都命中「真实数据指纹」→ "real_okx"
      - 否则 → "mixed"（半真半假，一般意味着环境配置有问题）
    """
    import time as _time
    root = root or DEFAULT_ROOT
    today_0_ms = int(_time.time() // 86400) * 86400 * 1000

    results: list[str] = []
    for tf in timeframes:
        p = Path(root) / f"{scene}__{tf.replace('/', '_')}.csv.gz"
        if not p.is_file():
            return "missing"
        with gzip.open(p, "rt", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            rows = list(rd)
        if not rows:
            return "missing"
        first = rows[0]
        last = rows[-1]
        ts0 = int(first["timestamp"])
        tsN = int(last["timestamp"])
        o0 = float(first["open"])
        if tsN < _EARLIEST_REAL_END_MS:
            return "missing"  # 文件时间跨度不合常理（占位小文件）
        # 合成指纹
        synth_hit = (
            abs(ts0 - _SYNTH_START_TS_MS) <= TF_SECONDS_MS.get(tf, 0)
            and abs(o0 - _SYNTH_BASE_PRICE) <= _SYNTH_BASE_PRICE * _SYNTH_START_OPEN_EPS * 2.0
        )
        # 真实指纹：首根 ts 早于今日 0 点、非合成锚点、末根不晚于今日 0 点+1 天
        real_hit = (
            ts0 < today_0_ms
            and abs(ts0 - _SYNTH_START_TS_MS) > TF_SECONDS_MS.get(tf, 0)
            and tsN < today_0_ms + 86400_000
        )
        if synth_hit and not real_hit:
            results.append("synthetic_gbm")
        elif real_hit and not synth_hit:
            results.append("real_okx")
        else:
            # 既不像合成也不像真实 → mixed（让外层显式提示）
            results.append("mixed")

    uniq = set(results)
    if len(uniq) == 1:
        return next(iter(uniq))  # type: ignore[return-value]
    if "real_okx" in uniq and "synthetic_gbm" in uniq:
        return "mixed"
    return "mixed"


TF_SECONDS_MS = {
    "1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000,
    "1h": 3600_000, "4h": 4 * 3600_000, "1d": 86400_000,
}


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
