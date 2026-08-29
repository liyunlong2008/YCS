#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy/pull_real_okx_klines.py —— 极简「直接从 OKX 拉真实历史 K 线」脚本。

修复记录：
  · v1 IndexError：首次 fetch 返回空越界
  · v1 time.mktime 本地时区 → v2 用 calendar.timegm 纯 UTC
  · v1 单页 limit=1200 silent truncate → v2 每页 100 拼页
  · v1 scene until 跨 TF 共用（超保留深度）→ v2 adjust_until_for_tf
  · v3(Win 实际跑失败修复 - 初版)：per-TF until lock / RETRY_EMPTY_PAGES / TOLERANCE_ROWS
  · v4(Win 实际跑失败修复 - 根因版)：
      (1) ccxt OKX fetch_ohlcv 的 params 键改为 **before**，而非 until（OKX v5
          history-candles 原生只认 before/after，传 until 被静默忽略→1m 拉到"正
          在生成的未归档最近 N 根"→返回 0 根）。
      (2) 去掉 per-page `ts<=cur_until` 的二次过滤：和 before 语义叠加时，
          偶发"最前一页末 N 根(5m=49 根)被截断"→导致 1151/1200 的缺口。
      (3) per-TF pad/tolerance：
            1m pad=2h(避开近 1h 未归档),  tolerance=5
            5m pad=4h,                         tolerance=3
           15m pad=8h,                         tolerance=1
            1h pad=1d,                         tolerance=1
            4h pad=1d,                         tolerance=1
            1d pad=3d,                         tolerance=0（日线必须完整）
      (4) SCENES 锁死 1d 窗口（trend_down FTX / trend_up BTC-ETF / range 大选）
          若 OKX 真拿不到 1200 根（ETH-USDT-SWAP 2019/2020 前稀疏），允许
          min_rows = max(600, need-300) = 900 根 写出 + WARN，保证 stage8 仍能
          拿首尾 900 根做 涨跌幅/振幅 阈值断言（900d ≈ 2.5y 足够覆盖牛熊窗口）。
      (5) 修正尾部 return 1 + 重复 `if __name__` 导致的 IndentationError。
"""
from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import math
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

# 每 TF 一根 bar 的毫秒
_TF_MS: dict[str, int] = {
    "1m":  60_000,
    "5m":  5 * 60_000,
    "15m": 15 * 60_000,
    "1h":  3_600_000,
    "4h":  4 * 3_600_000,
    "1d":  24 * 3_600_000,
}

_1D_MS = 24 * 3_600_000
# OKX history-candles v5 各 TF 实际最大历史深度（保守值）
# 注意：1m/5m 的实际 Win 返回 OKX 可能 < 官方宣称，取 stage13 里能通过的"~2 月 / ~3 月"保守值。
_OKX_MAX_RETENTION_MS: dict[str, int] = {
    "1m":  60  * _1D_MS,            # OKX 官方 1m≈60 天
    "5m":  90  * _1D_MS,            # 5m≈3 月
    "15m": 180 * _1D_MS,            # 15m≈6 月
    "1h":  400 * _1D_MS,            # 1h≈13 月
    "4h":  3 * 365 * _1D_MS,
    "1d":  6 * 365 * _1D_MS,        # 6y，保证 1200d=3.29y + 2024-01-10 场景(起点 2020-09) 仍落在保留深度内
}

# per-TF padding：避开"未收盘 + OKX 近 N 小时未归档的缺口"
#   · 1m 之前 30min pad 仍导致近 1h 的 1m 未返回，缺 49 ≈ 1h 的缺口 → 改 2h
#   · 5m 之前 2h pad 仍差 49，改 4h
_PAD_MS: dict[str, int] = {
    "1m":   2 * 3_600_000,          # 2h
    "5m":   4 * 3_600_000,          # 4h
    "15m":  8 * 3_600_000,          # 8h
    "1h":   1 * _1D_MS,
    "4h":   1 * _1D_MS,
    "1d":   3 * _1D_MS,             # 避开未收盘今日 + 昨日 21:00 UTC 切日线模糊
}

# per-TF 缺口容忍（≤ 这个值写出 warning 不 FAIL；stage8 1200 断言可能提示 warning）
_TOLERANCE_ROWS: dict[str, int] = {
    "1m":  5,
    "5m":  3,
    "15m": 1,
    "1h":  1,
    "4h":  1,
    "1d":  0,                       # 日线窗口必须完整，否则 stage8 趋势阈值失真
}

PER_PAGE = 100                     # OKX v5 每页最大 100（固定，不再随 remain 缩）
RETRY_EMPTY_PAGES = 8              # 连续空页容忍（8 页 × 1.1*per_page ≈ 880 bar 跨度，够跨 OKX 空洞）


def _utc_epoch_ms(ymd_hms: str) -> int:
    """'YYYY-MM-DD HH:MM:SS'（UTC）→ epoch ms。"""
    return calendar.timegm(time.strptime(ymd_hms, "%Y-%m-%d %H:%M:%S")) * 1000


# ---------------------------------------------------------------------------
# SCENES 定义
#   * 基准 until_ms：描述中对应真实事件（供日志提示 + 非锁 TF 回退）
#   * per_tf_until_ms：scene × tf 的锁死窗口，**仅对 1d 锁**：
#       - trend_up    1d → 2024-01-10（BTC ETF 通过后主升浪末端）
#       - trend_down  1d → 2022-11-10（FTX 破产当周）
#       - range       1d → 2024-10-10（美国大选前横盘末端）
#     其余 TF（1m/5m/15m/1h/4h）OKX 保留深度不够回退到事件窗口，所以不传
#     per_tf_until_ms → 自动走 adjust_until_for_tf 取『最近完整窗口』。
# ---------------------------------------------------------------------------
SCENES: dict[str, dict] = {
    "trend_up": {
        "until_ms": _utc_epoch_ms("2024-01-10 00:00:00"),
        "desc": "2023-10 → 2024-01 牛市主升浪（+~57%）",
        "per_tf_until_ms": {
            "1d": _utc_epoch_ms("2024-01-10 00:00:00"),
        },
        # 锁死窗口允许的最低行数（避免 FTX 2022 等 OKX 历史稀疏时直接 FAIL）
        "locked_min_rows": {
            "1d": 900,
        },
    },
    "trend_down": {
        "until_ms": _utc_epoch_ms("2022-11-20 00:00:00"),
        "desc": "2022-05 → 2022-11 LUNA+FTX 连环暴跌（-~60%）",
        "per_tf_until_ms": {
            "1d": _utc_epoch_ms("2022-11-10 00:00:00"),  # FTX 破产日
        },
        "locked_min_rows": {
            "1d": 600,   # trend_down 1d 2022-11 往前 OKX 数据相对稀少，600d≈1.6y 够暴跌判断
        },
    },
    "range": {
        "until_ms": _utc_epoch_ms("2024-10-10 00:00:00"),
        "desc": "2024-07 → 2024-10 美国大选前横盘（振幅 <9%）",
        "per_tf_until_ms": {
            "1d": _utc_epoch_ms("2024-10-10 00:00:00"),
        },
        "locked_min_rows": {
            "1d": 900,
        },
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
        if ts not in seen:
            seen[ts] = b
    return [seen[k] for k in sorted(seen)]


def fetch_ohlcv_paginated(
    ex,
    symbol: str,
    timeframe: str,
    *,
    need: int,
    until_ms: int,
    _per_page: int = PER_PAGE,
    _accept_less_than: int | None = None,
) -> list[list]:
    """多页拉取 OKX OHLCV，直到凑够 need 根 / 触底 / 空页达 RETRY_EMPTY_PAGES。

    v4 关键：
      · params 用 **before=cur_until_ms**（ccxt/OKX 原生语义，传 until 被静默忽略）。
      · 固定每页 limit=_per_page(100)（不再随 remain 缩小到 1→OKX 返回空）。
      · **不去除** page 中 >cur_until 的 bar：before 已保证 page[-1].ts<=before；
        再切一刀会让跨页边界 1151/1200 类缺口。
      · 空页回退：按 _per_page×tf_ms×1.1 大步回退；RETRY 提高到 8。
      · `_accept_less_than`：若 caller 指定（例如锁 FTX 1d 但 OKX 仅能 600 根），
        实际 >= _accept_less_than 即返回；caller 决定 warning。
    """
    if need <= 0:
        return []
    min_ok = need if _accept_less_than is None else min(_accept_less_than, need)
    min_ok = max(1, min_ok)

    accum: list[list] = []
    cur_until = int(until_ms)
    tf_ms = _TF_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(f"不支持的 timeframe: {timeframe}")
    now_ms = int(time.time() * 1000)
    if cur_until > now_ms:
        cur_until = now_ms

    empty_streak = 0
    # 迭代上限：理论页数 + 冗余。若指定了更低 min_ok，仍按 need 页数迭代（以便尽量填满）
    max_iter = int(math.ceil(need / _per_page)) + 30

    for _ in range(max_iter):
        if len(accum) >= need:
            break
        this_limit = _per_page
        try:
            page = ex.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=this_limit,
                # OKX v5 history-candles 原生 before/after; 不要用 until（被忽略）
                params={"before": str(cur_until)},
            ) or []
        except Exception:
            page = []
            for _i in range(2):
                time.sleep(0.7)
                try:
                    page = ex.fetch_ohlcv(
                        symbol,
                        timeframe=timeframe,
                        limit=this_limit,
                        params={"before": str(cur_until)},
                    ) or []
                except Exception:
                    page = []
                if page:
                    break

        if not page:
            empty_streak += 1
            if empty_streak >= RETRY_EMPTY_PAGES:
                break
            step_back = max(int(_per_page * tf_ms * 1.1), tf_ms * 10)
            cur_until -= step_back
            continue

        empty_streak = 0
        # v4: 不再 page 内部过滤 ts<=cur_until。before 已保证 page 末尾<=cur_until。
        # ccxt 统一升序返回：page[0] 最早, page[-1] 最晚（<= before）
        earliest_ts = int(page[0][0])
        latest_ts = int(page[-1][0])
        # 偶发死循环：cur_until 不变导致重复同一页 -> 若 earliest_ts >= cur_until 则手动后退
        if latest_ts >= cur_until:
            cur_until = latest_ts - 1
        accum = list(page) + accum
        next_until = earliest_ts - 1
        if next_until >= cur_until:
            next_until = cur_until - tf_ms
        if next_until < 0:
            next_until = 0
        cur_until = next_until

    accum = _dedup_bars(accum)

    tol = _TOLERANCE_ROWS.get(timeframe, 0)
    if len(accum) >= need:
        return accum[-need:]
    # 带缺口返回：caller 端 warning
    if len(accum) >= max(need - tol, min_ok):
        return accum
    # 仍不足 → 抛 RuntimeError；msg 包含『实际/需要/目标until/最终until』便于用户调试
    raise RuntimeError(
        f"OKX 返回不足：需要 {need} 根 {timeframe}，实际 {len(accum)} 根"
        f"（until={fmt_date(until_ms)}；若 tf=1m/5m/15m 请检查是否超出 OKX 保留深度）"
    )


def adjust_until_for_tf(until_ms: int, timeframe: str, *, need: int) -> int:
    """按 TF 的 retention + pad + need 窗口，把 scene until 夹进『有真实数据的合法区间』。

    修正语义（避免 stage13 1d 2024-01-10 被硬推到最近）：
      · 下限：若 until 过早 → `until - need_win` 会比 `now - retention` 还老，
        这种才把 until 前推到 `oldest_allowed_start + need_win`；
      · 上限：只在 until **太靠近现在**（未收盘 / 未归档）时才夹到
        `floored_now - pad - tf`；历史日期不改。
    """
    tf_ms = _TF_MS[timeframe]
    need_win_ms = int(need) * tf_ms
    retention = _OKX_MAX_RETENTION_MS[timeframe]
    pad_ms = _PAD_MS[timeframe]
    now_ms = int(time.time() * 1000)

    oldest_allowed_start = now_ms - retention
    until_lower_bound = oldest_allowed_start + need_win_ms
    # 上限：最近的一根『已完整 + 过了 pad』bar
    floored_now_ms = (now_ms // tf_ms) * tf_ms
    until_upper_bound = floored_now_ms - pad_ms - tf_ms

    effective_until = int(until_ms)
    # 只有"until 过早导致起点超出 retention"时才往上推
    if effective_until - need_win_ms < oldest_allowed_start:
        effective_until = until_lower_bound
    # 只有"until 太新，靠近未收盘区"才往下夹
    if effective_until > until_upper_bound:
        effective_until = until_upper_bound
    return int(effective_until)


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
        help=f"每文件目标根数（默认 {ROWS_PER_FILE}；stage8 默认断言 1200）",
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
    total_warn = 0
    total_fail = 0

    for scene, cfg in SCENES.items():
        scene_until = int(cfg["until_ms"])
        per_tf_until: dict[str, int] = {
            str(k): int(v) for k, v in (cfg.get("per_tf_until_ms") or {}).items()
        }
        locked_min_rows_cfg: dict[str, int] = {
            str(k): int(v) for k, v in (cfg.get("locked_min_rows") or {}).items()
        }
        print("")
        print(f"==== scene={scene}  ({cfg['desc']})  until(UTC base)={fmt_date(scene_until)} UTC")
        for tf in TIMEFRAMES:
            out_file = OUT_DIR / f"{scene}__{tf.replace('/', '_')}.csv.gz"
            try:
                if tf in per_tf_until:
                    fixed_until = per_tf_until[tf]
                    effective_until = fixed_until
                    print(f"  📌 {tf:<4} · per_tf_until 锁死 → {fmt_date(effective_until)} UTC")
                    fallback_adjust = True
                    accept_less = locked_min_rows_cfg.get(tf)
                else:
                    effective_until = adjust_until_for_tf(scene_until, tf, need=args.rows)
                    fallback_adjust = False
                    accept_less = None

                adjusted_msg = ""
                if effective_until != scene_until and not (tf in per_tf_until):
                    adjusted_msg = f"场景 until 超出 OKX 保留深度 → 调整为 {fmt_date(effective_until)} UTC"
                    print(f"  📦 {tf:<4} · {adjusted_msg}")

                ohlcv: list[list]
                try:
                    ohlcv = fetch_ohlcv_paginated(
                        ex, INST, timeframe=tf, need=args.rows, until_ms=effective_until,
                        _accept_less_than=accept_less,
                    )
                except RuntimeError as first_err:
                    # 锁死窗口第一次失败时：
                    #   (a) 先尝试把 accept_less 再放宽一次到 max(365, need-600)
                    #   (b) 再不行就 fallback 到自适应最近窗口
                    if not fallback_adjust:
                        raise
                    # (a) 放宽 accept_less
                    relaxed = max(365, args.rows - 600)
                    if accept_less is None or relaxed < accept_less:
                        try:
                            ohlcv = fetch_ohlcv_paginated(
                                ex, INST, timeframe=tf, need=args.rows, until_ms=effective_until,
                                _accept_less_than=relaxed,
                            )
                            warn_prefix = f"  🚑 {tf:<4} · 锁死窗口放宽 min_rows={relaxed} 成功"
                            print(warn_prefix)
                        except RuntimeError:
                            ohlcv = None  # 继续走 fallback 自适应
                    else:
                        ohlcv = None

                    if ohlcv is None:
                        # (b) fallback 自适应
                        second_until = adjust_until_for_tf(scene_until, tf, need=args.rows)
                        if second_until != effective_until:
                            print(f"  🚑 {tf:<4} · 锁死窗口失败（{first_err}），fallback 自适应 until={fmt_date(second_until)} 再试一次")
                            ohlcv = fetch_ohlcv_paginated(
                                ex, INST, timeframe=tf, need=args.rows, until_ms=second_until,
                            )
                            effective_until = second_until
                            adjusted_msg = f"fallback_adjust 自适应 {fmt_date(effective_until)}"
                        else:
                            raise

                got_rows = len(ohlcv)
                tol = _TOLERANCE_ROWS.get(tf, 0)
                warn_msg = ""
                if got_rows < args.rows:
                    diff = args.rows - got_rows
                    if accept_less is not None and got_rows >= accept_less:
                        warn_msg = f" ⚠️ 实际 {got_rows}/{args.rows}（锁死窗口 min={accept_less} OK）"
                    elif diff <= tol:
                        warn_msg = f" ⚠️ 实际 {got_rows}/{args.rows}（缺口 {diff} ≤ TOLERANCE={tol}）"
                    else:
                        # caller 不接受的缺口；理论 fetch_ohlcv_paginated 已抛，此处只是双保险
                        raise RuntimeError(
                            f"缺口过大：需要 {args.rows}，实际 {got_rows}（tol={tf}:{tol}）"
                        )

                write_csv_gz(out_file, ohlcv)
                size_kb = out_file.stat().st_size / 1024
                c0 = float(ohlcv[0][4]); c1 = float(ohlcv[-1][4])
                pct = (c1 - c0) / max(c0, 1e-12) * 100
                t0 = fmt_date(int(ohlcv[0][0])); t1 = fmt_date(int(ohlcv[-1][0]))
                mark = "WARN" if warn_msg else "OK "
                line = (f"  {mark} {tf:<4} → {out_file.name:<22}  {size_kb:>6.1f}KB  "
                        f"{t0} → {t1}   close {c0:.2f}→{c1:.2f}  Δ{pct:+.2f}%{warn_msg}")
                print(line)
                if warn_msg:
                    total_warn += 1
                else:
                    total_ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {tf:<4} → {type(exc).__name__}: {exc}")
                total_fail += 1
                continue

    try:
        ex.close()
    except Exception:
        pass

    # ---- 1D 自检（与 pytest stage8 对齐，只检查趋势/振幅是否足够） ----
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
        tol_1d = _TOLERANCE_ROWS.get("1d", 0)
        min_rows_1d = max(args.rows - tol_1d, 600)
        if len(rows) < min_rows_1d:
            print(f"  FAIL {scene:<10} 1d 条数 {len(rows)} << min={min_rows_1d}")
            all_pass = False
            continue
        closes = [float(r["close"]) for r in rows[-args.rows:]] if len(rows) >= args.rows else [float(r["close"]) for r in rows]
        c0, c1 = closes[0], closes[-1]
        pct = (c1 - c0) / max(c0, 1e-12) * 100
        amp = (max(closes) - min(closes)) / max(c0, 1e-12) * 100
        if scene == "trend_up":
            ok = pct >= scene_check["trend_up"]
            info = f"close Δ={pct:+.2f}%  need ≥+20%"
        elif scene == "trend_down":
            ok = pct <= scene_check["trend_down"]
            info = f"close Δ={pct:+.2f}%  need ≤-15%"
            if not ok:
                info += (
                    "  🚩 锁 FTX 窗口或 fallback 后仍不满足 ≤-15%。"
                    "请改 SCENES['trend_down']['per_tf_until_ms']['1d'] 为真正暴跌窗口末端。"
                )
        else:  # range
            ok = amp <= scene_check["range_amp"]
            info = f"振幅={amp:.2f}%  need ≤8%"
        mark = "OK " if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  {mark}  {scene:<10} {info}")

    print("")
    print(f"==== 完成：成功 {total_ok}，警告 {total_warn}，失败 {total_fail}。文件位置：{OUT_DIR}")
    print("下一步建议：git add tests/fixtures/market_data/*.csv.gz && git commit -m 'fixtures: 真实 OKX K 线' && git push")
    if not all_pass or total_fail > 0:
        print("⚠️ 存在失败/自检不通过项，push 前请检查代理 / 截止日期。", file=sys.stderr)
        return 1
    if total_warn > 0:
        print(
            f"ℹ️  存在 {total_warn} 条行数 warning（锁死窗口 min_rows 或 TOLERANCE 放宽）。"
            "若 stage8 红，请重跑本脚本或加 --rows 更大值再手动裁尾。",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
