#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy/pull_real_okx_klines.py —— 极简「直接从 OKX 拉真实历史 K 线」脚本。

修复记录（相对 v1）：
  · v1 IndexError：首次 fetch 返回空时 ohlcv[0][0] 越界 → v2 抽出 fetch_ohlcv_paginated，
     对空结果做 2 次重试，仍空抛 RuntimeError("OKX 返回 0 根…") 而不是 IndexError。
  · v1 time.mktime 用本地时区（Win 东八区 +8h，until 变未来时间 → OKX 返回空）
     → v2 用 calendar.timegm(strptime(UTC)) 生成纯 UTC epoch ms。
  · v1 单页 limit=1200：OKX v5 history-candles 各 TF 最大 limit=100，大 limit 被 truncate
     → v2 固定每页 100，while 拼页直到凑够 need 根 / 触底 / 超 RETRY_PAGES。
  · v1 所有 TF 共用 scene until_ms：trend_down 截止 2022-11，而 1m/5m 在 OKX 仅保留
     ~2 个月 → 永远凑不到 1200 根 → v2 引入 adjust_until_for_tf：根据 TF × need
     计算「覆盖 1200 根所需窗口」，若场景原 until 早于现在 − 安全边际（基于 OKX 官方
     history 深度表），自动回退到"现在 − safe_depth"作为新 until。
  · trend_down 1d 必须满足 stage8「跌幅 ≤ -15%」，因此 1d 保留原 LUNA/FTX 窗口不动；
     只有 1m/5m/15m/1h/4h 做自适应。

前置：
  · 你的本地机器 127.0.0.1:10808 开着代理（clash/v2ray/...），能过 OKX。
  · 项目根目录跑过 `uv sync`（ccxt 已在依赖里）。

用法：
  # 直接拉（默认代理 http://127.0.0.1:10808，覆盖重写 18 个 CSV.GZ）
  uv run python deploy/pull_real_okx_klines.py

  # 自定义代理（环境变量覆盖）
  OKX_PROXY=http://127.0.0.1:7890 uv run python deploy/pull_real_okx_klines.py

  # 拉完以后 commit 到仓库
  git add tests/fixtures/market_data/*.csv.gz
  git commit -m "fixtures: 真实 OKX ETH-USDT-SWAP K 线 3 场景 × 6 周期 × 1200 根"
  git push

产物（load_fixture / stage8 pytest / /api/ai/analyze 完全兼容）：
  tests/fixtures/market_data/<scene>__<tf>.csv.gz
  列：timestamp(ms), open, high, low, close, volume  升序
  每文件 1200 根 × 3 场景 × 6 周期 = 18 文件
"""
from __future__ import annotations

import argparse
import calendar
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
ROWS_PER_FILE = 1200              # stage8 断言用的条数，默认不改
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")

# 每 TF 一根 bar 的毫秒（用于计算窗口长度和 OKX 历史深度）
_TF_MS: dict[str, int] = {
    "1m":  60_000,
    "5m":  5 * 60_000,
    "15m": 15 * 60_000,
    "1h":  3_600_000,
    "4h":  4 * 3_600_000,
    "1d":  24 * 3_600_000,
}

# OKX history-candles v5 各 TF 实际最大历史深度（按 OKX 文档 & 用户实际可返回值留安全裕度）
#   · 1m  /  3m  官方：1 个月
#   · 5m  / 15m  官方：2-3 个月（实测更短，取保守 2 月 = 61 天）
#   · 1h  /  2h  官方：12 个月
#   · 4h        官方：约 24 个月
#   · 1d        官方：无限制（> 3 年）
# 用毫秒
_1D_MS = 24 * 3_600_000
_OKX_MAX_RETENTION_MS: dict[str, int] = {
    "1m":  31  * _1D_MS,
    "5m":  61  * _1D_MS,
    "15m": 120 * _1D_MS,   # 15m 稍保守 4 个月
    "1h":  365 * _1D_MS,
    "4h":  2 * 365 * _1D_MS,
    "1d":  5 * 365 * _1D_MS,
}
# 凑齐 need 根 + 一定安全裕度 → 最少需要覆盖的历史跨度
MIN_PAGES_PER_CALL = 100   # OKX v5 每页最大 100，我们固定按此请求避免"limit 1200 → 被截断"
RETRY_EMPTY_PAGES = 2     # 如果某页返回空，重试 RETRY_EMPTY_PAGES 次；仍空 → 判定触底


# =============================================================================
# 3 段真实历史时期（截止时间 = 纯 UTC，用 calendar.timegm，不再用本地时区）
# =============================================================================
def _utc_epoch_ms(ymd_hms: str) -> int:
    """'YYYY-MM-DD HH:MM:SS'（UTC）→ epoch ms。"""
    return calendar.timegm(time.strptime(ymd_hms, "%Y-%m-%d %H:%M:%S")) * 1000


SCENES = {
    "trend_up": {
        # 2023-10 → 2024-01 牛市主升浪（1d 约 +57%，stage8 要求 ≥ +20%）
        "until_ms": _utc_epoch_ms("2024-01-10 00:00:00"),
        "desc": "2023-10 → 2024-01 牛市主升浪（+~57%）",
    },
    "trend_down": {
        # 2022-05 → 2022-11 LUNA+FTX 连环暴跌（1d 约 -60%，stage8 要求 ≤ -15%）
        "until_ms": _utc_epoch_ms("2022-11-20 00:00:00"),
        "desc": "2022-05 → 2022-11 LUNA+FTX 连环暴跌（-~60%）",
    },
    "range": {
        # 2024-07 → 2024-10 美国大选前横盘（stage8 1d 振幅 ≤ 8%）
        "until_ms": _utc_epoch_ms("2024-10-10 00:00:00"),
        "desc": "2024-07 → 2024-10 美国大选前横盘（振幅 <9%）",
    },
}


# =============================================================================
# 模块级可测函数
# =============================================================================
def write_csv_gz(path: Path, ohlcv: list[list]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(ohlcv)


def fmt_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def _dedup_bars(bars: list[list]) -> list[list]:
    """按 ts_ms 去重并升序（偶发 OKX 跨页边界重复）。"""
    if not bars:
        return bars
    seen: dict[int, list] = {}
    for b in bars:
        ts = int(b[0])
        if ts not in seen:  # 保留首次出现者
            seen[ts] = b
    return [seen[k] for k in sorted(seen)]


def fetch_ohlcv_paginated(
    ex,
    symbol: str,
    timeframe: str,
    *,
    need: int,
    until_ms: int,
    _per_page: int = MIN_PAGES_PER_CALL,
) -> list[list]:
    """多页拉取 OKX OHLCV，直到凑够 need 根 / 触底 / 空页达到 RETRY_EMPTY_PAGES 次。

    关键修复：
      · 固定每页 limit=_per_page(100)，OKX 不再 silent truncate
      · 空首 / 中间空页不访问 ohlcv[0]，避免 IndexError，改做『计数+重算 until』式重试
      · 返回长度等于 need；不够 → RuntimeError("OKX 返回不足：实 N 根，缺 N−len 根")
    """
    if need <= 0:
        return []
    accum: list[list] = []
    cur_until = int(until_ms)
    # 边界保护：until 不得晚于"当前 UTC + 1 根 TF bar"（OKX 会把未来 until 当最近一根）
    tf_ms = _TF_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(f"不支持的 timeframe: {timeframe}")
    now_plus_one = int(time.time() * 1000) + tf_ms
    if cur_until > now_plus_one:
        cur_until = now_plus_one

    empty_streak = 0
    max_iter = (need // _per_page) + 20  # 理论页数 + 安全冗余上限（防死循环）
    for _ in range(max_iter):
        if len(accum) >= need:
            break
        remain = need - len(accum)
        this_limit = min(_per_page, max(remain, 1))
        try:
            page = ex.fetch_ohlcv(
                symbol, timeframe=timeframe, limit=this_limit,
                params={"until": str(cur_until)},
            ) or []
        except Exception as _e:
            # 网络抖动 / 限流导致的单次异常，重试一次再算失败
            page = []
            for _i in range(2):
                time.sleep(1.0)
                try:
                    page = ex.fetch_ohlcv(
                        symbol, timeframe=timeframe, limit=this_limit,
                        params={"until": str(cur_until)},
                    ) or []
                except Exception:
                    page = []
                if page:
                    break
        if not page:
            empty_streak += 1
            if empty_streak >= RETRY_EMPTY_PAGES:
                # 连续两页空，判定触底
                break
            # 单页空可能是 until 刚好落在无 bar 间隙 → 往前推 2 根再试
            cur_until = cur_until - tf_ms * 2
            continue
        empty_streak = 0
        # 去重本页 & 过滤时间戳大于当前 until 的 bar
        page = [b for b in page if int(b[0]) <= cur_until]
        if not page:
            empty_streak += 1
            cur_until -= tf_ms * 2
            continue
        # ccxt 返回升序：page[-1] 时间最大 / 最靠近 until
        earliest_ts = int(page[0][0])
        latest_ts = int(page[-1][0])
        # 加到 accum 前边（更老的那一边）—— 保持 accum 整体升序
        accum = list(page) + accum
        # 下一页 until 要跳到「本页最早一根之前 1 tick」
        next_until = earliest_ts - 1
        if next_until >= cur_until:
            # 防死循环（时间戳未推进）：强制减 1
            next_until = cur_until - tf_ms
        cur_until = next_until
        if earliest_ts <= 0 or earliest_ts >= latest_ts:
            # 再防：只剩 ≤ 1 根 且时间未推进 → break
            pass
    # 最终全局去重（跨页边界重复）+ 升序裁剪
    accum = _dedup_bars(accum)
    if len(accum) < need:
        raise RuntimeError(
            f"OKX 返回不足：需要 {need} 根 {timeframe}，实际 {len(accum)} 根"
            f"（until={fmt_date(until_ms)}；若 tf=1m/5m/15m 请检查是否超出 OKX 保留深度）"
        )
    # 裁剪为最末 need 根（即最靠近 until 的 need 根），保证 stage8 断言窗口对齐
    accum = accum[-need:]
    assert len(accum) == need
    return accum


def adjust_until_for_tf(until_ms: int, timeframe: str, *, need: int) -> int:
    """按 TF 的 OKX 保留深度，自适应 scene until。

    原则：
      ① 若原 until 导致「need 根窗口」有任意一段超出 OKX 可返回最大保留深度
         → 推 until 到"最近合法窗口"：until = now - 1d_pad - tf_ms
         相应 need_win = (now - until - need*tf_ms ... now - until − tf_ms) 整段全落在
         retention 范围内，OKX 必然有对应深度 bars。
      ② 1d 是例外（保留深度极大），为保证 stage8 trend_down LUNA/FTX 暴跌窗口不被改，
         对 1d 直接原样返回
      ③ 任何调整都不能把 until 推到未来（即返回值 ≤ now − 1_bar_ms）
    """
    if timeframe == "1d":
        return int(until_ms)
    tf_ms = _TF_MS[timeframe]
    need_win_ms = int(need) * tf_ms
    retention = _OKX_MAX_RETENTION_MS.get(timeframe)
    if retention is None:
        raise ValueError(f"未知 timeframe={timeframe}")
    pad_ms = _1D_MS
    now_ms = int(time.time() * 1000)

    # 按原 until：整段窗口 [until - need_win_ms, until] 的最远点是 until - need_win_ms
    # 合法 iff until - need_win_ms >= now - retention
    # 等价于 until >= now - retention + need_win_ms
    oldest_allowed_start = now_ms - retention
    until_lower_bound = oldest_allowed_start + need_win_ms
    if int(until_ms) >= until_lower_bound and int(until_ms) <= now_ms - tf_ms:
        return int(until_ms)

    # 需要推近：选"最近的一段合法窗口"的末端
    new_until = now_ms - pad_ms - tf_ms
    # 上限保护
    cap = now_ms - tf_ms
    if new_until > cap:
        new_until = cap
    return int(new_until)


# =============================================================================
# main
# =============================================================================
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
        scene_until = int(cfg["until_ms"])
        print("")
        print(f"==== scene={scene}  ({cfg['desc']})  until(UTC base)={fmt_date(scene_until)} UTC")
        for tf in TIMEFRAMES:
            out_file = OUT_DIR / f"{scene}__{tf.replace('/', '_')}.csv.gz"
            try:
                # v2 fix: 根据 TF 自适应 until；1d 保持不变（LUNA/FTX 暴跌窗口保留）
                effective_until = adjust_until_for_tf(scene_until, tf, need=args.rows)
                if effective_until != scene_until:
                    print(f"  📦 {tf:<4} · 场景 until 超出 OKX 历史保留深度 → 调整为 {fmt_date(effective_until)} UTC")
                ohlcv = fetch_ohlcv_paginated(
                    ex, INST, timeframe=tf, need=args.rows, until_ms=effective_until,
                )
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
        if len(rows) < args.rows:
            print(f"  FAIL {scene:<10} 1d 条数 {len(rows)} < {args.rows}（stage8 会失败）")
            all_pass = False
            continue
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
