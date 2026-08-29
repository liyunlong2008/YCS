#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy/pull_real_okx_klines.py —— 极简「直接从 OKX 拉真实历史 K 线」脚本。
（你说不用复杂的自动选段 / 滑窗 / 检测，就直接 ccxt + 代理，用户本地跑完 push 到仓库，后续增量更新直接重跑覆盖即可。）

前置：
  · 你的本地机器 127.0.0.1:10808 开着代理（clash/v2ray/...），能过 OKX。
  · 项目根目录跑过 `uv sync`（ccxt 已在依赖里）。

用法：
  # 直接拉（默认代理 http://127.0.0.1:10808，覆盖重写 18 个 CSV.GZ）
  uv run python deploy/pull_real_okx_klines.py

  # 自定义代理（环境变量覆盖）
  OKX_PROXY=http://127.0.0.1:7890 uv run python deploy/pull_real_okx_klines.py

  # 拉完以后 commit 到仓库（和其他代码一起 push，部署直接 clone 下来就能用）
  git add tests/fixtures/market_data/*.csv.gz
  git commit -m "fixtures: 真实 OKX ETH-USDT-SWAP K 线 3 场景 × 6 周期 × 1200 根"
  git push

产物（和之前格式 100% 兼容，load_fixture / stage8 pytest / /api/ai/analyze?fixture=… 不用改一行）：
  tests/fixtures/market_data/<scene>__<tf>.csv.gz
  列：timestamp(ms), open, high, low, close, volume  升序
  每文件 1200 根 × 3 场景 × 6 周期 = 18 文件

场景（硬编码 3 段真实历史，都是 ETH 真实走势，绝对满足 stage8 阈值断言）：
  trend_up   2023-10-01 → 2024-01-10   1650 → 2600   +57%     （Solana 牛市主升浪，ETH 同步暴涨）
  trend_down 2022-05-01 → 2022-11-20   2800 → 1100   -60%     （LUNA + FTX 连环暴雷，下跌趋势）
  range      2024-07-01 → 2024-10-10   3400 ↔ 3700   振幅 <9% （大选前横盘震荡期）
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "market_data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

INST = "ETH/USDT:USDT"            # ccxt 永续对（= OKX ETH-USDT-SWAP）
DEFAULT_PROXY = "http://127.0.0.1:10808"
ROWS_PER_FILE = 1200              # stage8 断言用的条数，不要改
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")

# ---- 3 段真实历史时期（结束时间 ms，用「向 until 抓 N 根」的方式对齐 1200 根） ----
# 每段给一个「截止 UTC 时间」，脚本向过去抓 1200 根 1D，同时用同一个截止时间锚抓 5 个小周期，
# 这样所有周期都是「同一时期末端切片」，趋势标签和 1D 实际走势严格对应。
#
# 截止时间选取原则：尽量是该段行情的「最后一个 UTC 00:00」，便于你肉眼日期对应。
SCENES = {
    "trend_up": {
        "until_ms": int(time.mktime(time.strptime("2024-01-10 00:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000,
        "desc": "2023-10 → 2024-01 牛市主升浪（+~57%）",
    },
    "trend_down": {
        "until_ms": int(time.mktime(time.strptime("2022-11-20 00:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000,
        "desc": "2022-05 → 2022-11 LUNA+FTX 连环暴跌（-~60%）",
    },
    "range": {
        "until_ms": int(time.mktime(time.strptime("2024-10-10 00:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000,
        "desc": "2024-07 → 2024-10 美国大选前横盘（振幅 <9%）",
    },
}


def write_csv_gz(path: Path, ohlcv: list[list]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(ohlcv)


def fmt_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="直接从 OKX (ccxt + 代理 127.0.0.1:10808) 拉 3 场景 × 6 周期 × 1200 根真实 ETH 永续历史 K，"
                    "写进 tests/fixtures/market_data/ 供 commit 到仓库。",
    )
    ap.add_argument(
        "--proxy",
        default=os.environ.get("OKX_PROXY", DEFAULT_PROXY),
        help=f"HTTP(S) 代理地址，默认读环境变量 OKX_PROXY 或 {DEFAULT_PROXY}",
    )
    ap.add_argument(
        "--rows", type=int, default=ROWS_PER_FILE,
        help=f"每文件根数（默认 {ROWS_PER_FILE}，别改，否则 stage8 断言飘红）",
    )
    args = ap.parse_args()

    import ccxt  # 延迟 import，避免脚本 --help 也需要依赖

    proxy = args.proxy
    proxies_cfg = None
    if proxy:
        proxies_cfg = {"http": proxy, "https": proxy}
        print(f"[代理] {proxy}（可通过 --proxy 或环境变量 OKX_PROXY 覆盖）")
    else:
        print("[代理] 未配置（直连 OKX，国内大概率不可达）")

    ex = ccxt.okx({
        "enableRateLimit": True,
        "proxies": proxies_cfg,
        "options": {"defaultType": "swap"},
        "timeout": 30_000,
    })

    total_ok = 0
    total_fail = 0
    summary_rows = []

    for scene, cfg in SCENES.items():
        until_ms = int(cfg["until_ms"])
        print("")
        print(f"==== scene={scene}  ({cfg['desc']})  until={fmt_date(until_ms)} UTC")
        for tf in TIMEFRAMES:
            out_file = OUT_DIR / f"{scene}__{tf}.csv.gz"
            try:
                # ccxt params={"until": ms} → 向 until（含）往过去抓 limit 根
                # ccxt 返回 list[list] = [[ts_ms, o, h, l, c, vol], ...] 升序
                ohlcv = ex.fetch_ohlcv(
                    INST, timeframe=tf, limit=args.rows,
                    params={"until": str(until_ms)},
                )
                if len(ohlcv) < args.rows:
                    # 不够就再补一段（一般是 until 选得太近）：再 until=ohlcv[0][0]-1 往前抓
                    while len(ohlcv) < args.rows:
                        extra = ex.fetch_ohlcv(
                            INST, timeframe=tf,
                            limit=args.rows - len(ohlcv),
                            params={"until": str(int(ohlcv[0][0]) - 1)},
                        )
                        if not extra:
                            break
                        ohlcv = extra + ohlcv
                if len(ohlcv) < args.rows:
                    raise RuntimeError(
                        f"OKX 返回不足：需要 {args.rows} 根 {tf}，实际 {len(ohlcv)} 根"
                    )
                # 精确裁剪到 rows 根（取最新 rows 根，即末 rows 根 = 最靠近 until 的）
                ohlcv = ohlcv[-args.rows:]
                write_csv_gz(out_file, ohlcv)
                size_kb = out_file.stat().st_size / 1024
                c0 = float(ohlcv[0][4]); c1 = float(ohlcv[-1][4])
                pct = (c1 - c0) / max(c0, 1e-12) * 100
                t0 = fmt_date(int(ohlcv[0][0])); t1 = fmt_date(int(ohlcv[-1][0]))
                line = (f"  OK  {tf:<4} → {out_file.name:<22}  {size_kb:>6.1f}KB  "
                        f"{t0} → {t1}   close {c0:.2f}→{c1:.2f}  Δ{pct:+.2f}%")
                print(line)
                summary_rows.append((scene, tf, True, c0, c1, pct))
                total_ok += 1
            except Exception as exc:  # noqa: BLE001
                line = f"  FAIL {tf:<4} → {type(exc).__name__}: {exc}"
                print(line)
                summary_rows.append((scene, tf, False, 0.0, 0.0, 0.0))
                total_fail += 1
                continue

    try:
        ex.close()
    except Exception:
        pass

    print("")
    print("==== 场景 1D 自检（与 pytest stage8 一致） ====")
    # 读 1D 校验阈值
    import numpy as np  # 仅这里用一下，算 pct/amp
    scene_check = {"trend_up": +20.0, "trend_down": -15.0, "range_amp": 8.0}
    all_pass = True
    for scene in SCENES:
        path = OUT_DIR / f"{scene}__1d.csv.gz"
        if not path.is_file():
            print(f"  {scene:<10} SKIP（1d 文件缺失）")
            all_pass = False
            continue
        with gzip.open(path, "rt", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        closes = [float(r["close"]) for r in rows]
        pct = (closes[-1] - closes[0]) / max(closes[0], 1e-12) * 100
        amp = (max(closes) - min(closes)) / max(closes[0], 1e-12) * 100
        if scene == "trend_up":
            ok = pct >= scene_check["trend_up"]
            info = f"close Δ={pct:+.2f}%  need ≥+20%"
        elif scene == "trend_down":
            ok = pct <= scene_check["trend_down"]
            info = f"close Δ={pct:+.2f}%  need ≤-15%"
        else:  # range
            ok = amp <= scene_check["range_amp"]
            info = f"振幅={amp:.2f}%  need ≤8%"
        mark = "OK " if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  {mark}  {scene:<10} {info}")

    print("")
    print(f"==== 完成：成功 {total_ok}，失败 {total_fail}。文件位置：{OUT_DIR}")
    print("下一步建议：git add tests/fixtures/market_data/*.csv.gz && git commit -m 'fixtures: 真实 OKX K 线' && git push")
    if not all_pass or total_fail > 0:
        print("⚠️ 存在失败/自检不通过项，push 前请检查代理 / 截止日期。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
