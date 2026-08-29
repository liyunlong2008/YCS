"""
deploy/fetch_market_fixtures.py —— 拉取 **真实 OKX 历史 K 线** 作为离线 Fixtures（golden dataset）。

核心承诺（用户要求"一定要真实历史 K"，2026-08-29 起强制执行）：
  · 默认：必须真实。全部 18 个文件（3 场景 × 6 周期 × 1200 根）均来自 OKX 公共 REST
    `GET /api/v5/market/history-candles`，无需 API Key。
  · 只有显式传 `--allow-synth` 时，才允许回退到旧确定性 GBM 合成（纯应急）。
  · 自动挑选 3 段真实 ETH 历史时期作为 scene 标签的锚点（按 1D 1200 根 ≈ 3.3 年窗口）：
      trend_up  窗口涨跌幅 ≥ +20%（取最大涨幅窗口）
      trend_down 窗口涨跌幅 ≤ -15%（取最大跌幅窗口）
      range     窗口振幅   ≤ 8% （取最小振幅窗口）
  · 其余周期（1m / 5m / 15m / 1h / 4h）以 1D 窗口"最后一根 K 线时间戳"为终点锚，
    向过去抓 1200 根，确保同一 scene 下所有 TF 都是同一时期的末端高分辨率切片。

产物：tests/fixtures/market_data/<scene>__<tf>.csv.gz
      列：timestamp(ms), open, high, low, close, volume  （timestamp 升序）
      volume 单位：合约张数（OKX ETH-USDT-SWAP 原始定义，1 张 = 0.01 ETH），非负。

使用：
  # 生产环境 / VPS（一定能访问 OKX）：真实 K 线，缺失或 --force 就抓
  uv run python deploy/fetch_market_fixtures.py
  uv run python deploy/fetch_market_fixtures.py --force

  # 本地完全无法访问 OKX 时应急：允许合成兜底（不推荐，仅临时用）
  uv run python deploy/fetch_market_fixtures.py --allow-synth

  # 自定义每文件条数 / 代理
  ROWS_PER_TF=2000 HTTPS_PROXY=http://127.0.0.1:18080 \
    uv run python deploy/fetch_market_fixtures.py --force
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, NamedTuple

import numpy as np  # 仅合成路径需要；真实路径不依赖 numpy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "market_data"
SCENES: tuple[str, ...] = ("trend_up", "trend_down", "range")
# OKX REST bar 枚举直接对应本项目 TF（注意大小写：1H / 4H / 1D）
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")
OKX_BAR_BY_TF = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1H", "4h": "4H", "1d": "1D",
}
TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

# 1D 级窗口选段阈值（与 pytest stage8 断言一致）
WIN_ROWS_1D = 1200                # 1200 根 1D ≈ 3.3 年
MIN_TREND_UP_PCT   = +20.0 / 100  # 涨幅 ≥ 20%
MIN_TREND_DOWN_PCT = -15.0 / 100  # 跌幅 ≤ -15%
MAX_RANGE_AMP_PCT  =   8.0 / 100  # 振幅 ≤ 8%

# 合成路径配置（仅 --allow-synth）
BASE_PRICE = 2000.0
SEED_BY_SCENE = {"trend_up": 2026010101, "trend_down": 2026010102, "range": 2026010103}

INST_ID = "ETH-USDT-SWAP"
OKX_BASE = "https://www.okx.com/api/v5/market/history-candles"
PER_CALL_LIMIT = 100   # OKX history-candles 单请求最大 100 根
REQ_TIMEOUT_S = 20
REQ_RETRY = 3
REQ_RETRY_BACKOFF_S = 2.0


# ---------------------------------------------------------------------------
# 1) 真实抓取：纯 urllib 走 OKX 公共 history-candles REST（无 ccxt、无密钥）
# ---------------------------------------------------------------------------
class WindowInfo(NamedTuple):
    scene: str
    start_idx: int            # 1D 完整数组中窗口起点
    end_idx_exclusive: int    # 1D 完整数组中窗口终点（不含）
    start_ms: int
    end_ms: int               # 最后 1 根 timestamp_ms（也是其他 TF 的 before 锚）
    pct: float                # (close-end - close-start) / close-start
    amp: float                # (max_high - min_low) / close-start


def _okx_proxy_handler() -> urllib.request.ProxyHandler | None:
    """从标准环境变量构造 urllib ProxyHandler；无则返回 None（直连）。"""
    env = os.environ
    proxy: dict[str, str] = {}
    for proto, keys in (
        ("http",  ("HTTP_PROXY",  "http_proxy",  "ALL_PROXY", "all_proxy")),
        ("https", ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")),
    ):
        for k in keys:
            v = env.get(k)
            if v:
                proxy[proto] = v
                break
    return urllib.request.ProxyHandler(proxy) if proxy else None


def _http_get_json(url: str) -> dict:
    handler = _okx_proxy_handler()
    opener = urllib.request.build_opener(handler) if handler else urllib.request.build_opener()
    last_err: Exception | None = None
    for attempt in range(1, REQ_RETRY + 1):
        try:
            with opener.open(url, timeout=REQ_TIMEOUT_S) as resp:
                payload = resp.read()
                return json.loads(payload.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < REQ_RETRY:
                time.sleep(REQ_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(f"OKX REST 连续 {REQ_RETRY} 次失败: url={url[:120]} last_err={last_err}")


def fetch_okx_history(tf: str, rows: int, before_ms: int | None = None) -> list[list[float]]:
    """向过去抓取 rows 根 OKX K 线。若指定 before_ms，则时间戳严格 < before_ms。

    返回：[[ts_ms, o, h, l, c, vol], ...]，升序（最旧在前），长度严格等于 rows。
    vol 单位：合约张数（OKX data[5]，直接转 float，非负）。
    """
    bar = OKX_BAR_BY_TF[tf]
    collected_desc: list[list[float]] = []   # OKX 原始返回是降序（最新在前）
    after: str | None = str(before_ms) if before_ms else None
    target = rows
    while len(collected_desc) < target:
        params = {
            "instId": INST_ID,
            "bar": bar,
            "limit": str(min(PER_CALL_LIMIT, target - len(collected_desc))),
        }
        if after:
            params["after"] = after   # after = 小于此 ts_ms 的更老数据
        qs = urllib.parse.urlencode(params)
        j = _http_get_json(f"{OKX_BASE}?{qs}")
        if str(j.get("code", "")) != "0":
            raise RuntimeError(f"OKX 业务错误 code={j.get('code')} msg={j.get('msg')} bar={bar}")
        batch_raw = j.get("data") or []
        if not batch_raw:
            break
        batch_rows = []
        for r in batch_raw:
            # [ts_ms, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            ts_ms = int(r[0])
            o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4])
            vol = max(float(r[5]), 0.0)
            batch_rows.append([float(ts_ms), o, h, l, c, vol])
        collected_desc.extend(batch_rows)
        # 下一页取比当前最后一条（最老）还老的
        after = str(int(collected_desc[-1][0]) - 1)
        if len(batch_rows) < PER_CALL_LIMIT:
            break

    if len(collected_desc) < rows:
        raise RuntimeError(
            f"OKX tf={tf} 历史数据不足：需要 {rows} 实际拿到 {len(collected_desc)} "
            f"(before_ms={before_ms})"
        )

    # 取前 rows 条（= 最新 rows 条；collected_desc 是降序，前面是最新）→ 反转成升序
    newest_rows_desc = collected_desc[:rows]
    newest_rows_desc.reverse()  # → 升序
    # 类型统一成 int ts + float
    return [
        [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
        for r in newest_rows_desc
    ]


# ---------------------------------------------------------------------------
# 2) 滑动窗口选段：从 1D 长期历史里挑出 3 个合规场景
# ---------------------------------------------------------------------------
def pick_scene_windows(daily_ohlcv_asc: list[list[float]]) -> dict[str, WindowInfo]:
    """daily_ohlcv_asc：1D 升序，长度 ≥ WIN_ROWS_1D。返回 3 个场景。"""
    if len(daily_ohlcv_asc) < WIN_ROWS_1D + 100:
        raise RuntimeError(
            f"1D 历史不足：至少需要 {WIN_ROWS_1D + 100} 根用于选段，实际 {len(daily_ohlcv_asc)}"
        )
    closes = np.array([r[4] for r in daily_ohlcv_asc], dtype=np.float64)
    highs  = np.array([r[2] for r in daily_ohlcv_asc], dtype=np.float64)
    lows   = np.array([r[3] for r in daily_ohlcv_asc], dtype=np.float64)
    ts_arr = np.array([int(r[0]) for r in daily_ohlcv_asc], dtype=np.int64)

    W = WIN_ROWS_1D
    N = len(daily_ohlcv_asc)
    # 滑动窗口从 [0:W) 到 [N-W:N)；为了"标签越新越可用于近期回测"，优先选靠后（更近期）的窗口
    # 所以我们从右往左扫，第一个满足阈值约束的合格窗口里挑"极值"。
    best: dict[str, tuple[int, float, float]] = {}
    # scene -> (idx, score, amp)  score 定义：
    #   trend_up: pct_change（越大越好）
    #   trend_down: pct_change（越小越好）
    #   range: 振幅（越小越好）
    for i in range(N - W, -1, -1):
        c0 = closes[i]; c1 = closes[i + W - 1]
        pct = (c1 - c0) / max(c0, 1e-12)
        hi = float(highs[i:i + W].max())
        lo = float(lows[i:i + W].min())
        amp = (hi - lo) / max(c0, 1e-12)

        if pct >= MIN_TREND_UP_PCT:
            prev = best.get("trend_up")
            if prev is None or pct > prev[1]:
                best["trend_up"] = (i, float(pct), float(amp))
        if pct <= MIN_TREND_DOWN_PCT:
            prev = best.get("trend_down")
            if prev is None or pct < prev[1]:
                best["trend_down"] = (i, float(pct), float(amp))
        if amp <= MAX_RANGE_AMP_PCT:
            prev = best.get("range")
            if prev is None or amp < prev[2]:
                best["range"] = (i, float(pct), float(amp))

        # 提前退出：三个场景都找齐了，而且我们从右往左扫，最右一段本身就是近期最优
        if len(best) == 3:
            break

    if len(best) < 3:
        missing = [s for s in SCENES if s not in best]
        raise RuntimeError(f"真实 1D 历史里挑不出 3 个场景窗口，缺失: {missing}（最后一次 best={best}）")

    windows: dict[str, WindowInfo] = {}
    for s in SCENES:
        i, pct, amp = best[s]
        j = i + W  # 独占 end
        start_ms = int(ts_arr[i])
        end_ms   = int(ts_arr[j - 1])
        windows[s] = WindowInfo(
            scene=s, start_idx=i, end_idx_exclusive=j,
            start_ms=start_ms, end_ms=end_ms, pct=float(pct), amp=float(amp),
        )
    return windows


# ---------------------------------------------------------------------------
# 3) 合成器（仅 --allow-synth）
# ---------------------------------------------------------------------------
def synthesize_scene_tf(scene: str, tf: str, rows: int, base_price: float = BASE_PRICE) -> list[list]:
    rng = np.random.default_rng(SEED_BY_SCENE[scene])
    bar_sec = TF_SECONDS[tf]
    start_ts_ms = 1767225600_000  # 2026-01-01 00:00 UTC 锚点
    if scene == "trend_up":
        mu_per_bar = 0.00033; sigma = 0.004
    elif scene == "trend_down":
        mu_per_bar = -0.00020; sigma = 0.0045
    else:  # range
        mu_per_bar = 0.0;      sigma = 0.0022

    z = rng.standard_normal(rows)
    if scene == "range":
        log_px = np.log(base_price)
        log_px_arr = np.zeros(rows)
        for i in range(rows):
            pull = -0.08 * (log_px - np.log(base_price))
            log_px += (mu_per_bar + pull + sigma * z[i])
            log_px_arr[i] = log_px
        closes = np.exp(log_px_arr)
    else:
        log_paths = np.cumsum(mu_per_bar + sigma * z) + np.log(base_price)
        closes = np.exp(log_paths)

    opens = np.empty(rows)
    opens[0] = base_price * (1 + float(rng.uniform(-0.0005, 0.0005)))
    opens[1:] = closes[:-1]
    wick_up = np.abs(rng.normal(loc=sigma * 0.6, scale=sigma * 0.3, size=rows)) + 1e-8
    wick_dn = np.abs(rng.normal(loc=sigma * 0.6, scale=sigma * 0.3, size=rows)) + 1e-8
    highs = np.maximum(opens, closes) * (1 + wick_up)
    lows  = np.minimum(opens, closes) * (1 - wick_dn)

    bars_per_day = 86400 // bar_sec
    if bars_per_day > 0:
        phase = (np.arange(rows) % bars_per_day) / bars_per_day
        diurnal = 0.6 + 0.9 * np.exp(-((phase - 0.62) ** 2) / 0.01) + 0.5 * np.exp(-((phase - 0.30) ** 2) / 0.02)
    else:
        diurnal = np.ones(rows)
    base_vol = 1500.0 if tf in ("1m", "5m", "15m") else 8000.0 if tf == "1h" else 30000.0 if tf == "4h" else 180000.0
    volumes = np.clip(base_vol * diurnal * rng.lognormal(0.0, 0.45, size=rows), 0.001, None)
    timestamps_ms = start_ts_ms + np.arange(rows, dtype=np.int64) * bar_sec * 1000

    out: list[list] = []
    for i in range(rows):
        out.append([
            int(timestamps_ms[i]), float(f"{opens[i]:.6f}"), float(f"{highs[i]:.6f}"),
            float(f"{lows[i]:.6f}"), float(f"{closes[i]:.6f}"), float(f"{volumes[i]:.6f}"),
        ])
    return out


# ---------------------------------------------------------------------------
# 4) CSV.GZ 写入（升序）
# ---------------------------------------------------------------------------
def write_csv_gz(path: Path, rows: Iterable[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


# ---------------------------------------------------------------------------
# 5) 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="抓取真实 OKX 历史 K 线 Fixtures（默认禁止合成；--allow-synth 才应急兜底）",
    )
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件重拉")
    ap.add_argument("--rows", type=int, default=1200, help="每 TF 条数（默认 1200，= stage8 断言）")
    ap.add_argument(
        "--allow-synth", action="store_true",
        help="OKX 不可达时允许回退到确定性 GBM 合成（默认禁止；真实环境一般不要用）",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUT_DIR}  rows/TF={args.rows}  force={args.force}  allow_synth={args.allow_synth}")

    # =========================================================================
    # A. 真实路径：1D 选 3 段 → 各 TF 按锚点拉 1200 根
    # =========================================================================
    windows: dict[str, WindowInfo] | None = None
    daily_1d: list[list[float]] | None = None
    try:
        print("[1/3] 先拉 1D 长历史（最近 5~6 年）用于 3 场景自动选段...")
        daily_1d = fetch_okx_history("1d", rows=max(WIN_ROWS_1D + 150, 2000))  # 至少拿 2000 根 1D
        print(f"      1D 总数={len(daily_1d)}  区间: {daily_1d[0][0]} → {daily_1d[-1][0]} "
              f"(close {daily_1d[0][4]:.2f} → {daily_1d[-1][4]:.2f})")
        windows = pick_scene_windows(daily_1d)
        for s in SCENES:
            w = windows[s]
            # 日期（秒级）转本地可读
            s0 = time.strftime("%Y-%m-%d", time.gmtime(w.start_ms / 1000))
            s1 = time.strftime("%Y-%m-%d", time.gmtime(w.end_ms / 1000))
            print(f"      scene={s:<10} 窗口={s0}~{s1} Δ={w.pct*100:+.2f}% 振幅={w.amp*100:.2f}%")
    except Exception as exc:
        msg = f"[真实抓取] 1D 选段失败: {type(exc).__name__}: {exc}"
        if not args.allow_synth:
            print(f"ERROR: {msg}\n       （传 --allow-synth 才允许合成兜底，默认禁止）", file=sys.stderr)
            return 2
        print(f"WARN:  {msg} → 回退合成（--allow-synth 已启用）")

    used_real = False
    used_synth = False

    # 1D 缓存切片（只切一次，避免对每个 scene 再拉）
    daily_slice_by_scene: dict[str, list[list[float]]] = {}
    if windows is not None and daily_1d is not None:
        for s in SCENES:
            w = windows[s]
            daily_slice_by_scene[s] = daily_1d[w.start_idx:w.end_idx_exclusive]

    print("[2/3] 按场景 × 周期写入 CSV.GZ ...")
    need_realtime = any(scene for scene in SCENES for tf in TIMEFRAMES if not (
        (OUT_DIR / f"{scene}__{tf}.csv.gz").is_file()
    ) or args.force)

    for si, scene in enumerate(SCENES, start=1):
        for ti, tf in enumerate(TIMEFRAMES, start=1):
            fname = f"{scene}__{tf}.csv.gz"
            dst = OUT_DIR / fname
            if dst.is_file() and not args.force:
                print(f"   ({si}.{ti}) SKIP  {fname}（已存在，--force 覆盖）")
                continue

            ohlcv: list[list] | None = None
            if windows is not None and daily_1d is not None:
                try:
                    if tf == "1d":
                        # 直接用缓存的 1D 切片 → 保证趋势特征严格等于选段计算值
                        rows_src = daily_slice_by_scene[scene]
                        if len(rows_src) != args.rows:
                            # 若 --rows 非默认 1200，则按 end_ms 重新拉 args.rows 根
                            rows_src = fetch_okx_history(tf, rows=args.rows, before_ms=int(windows[scene].end_ms + 1))
                        ohlcv = rows_src
                    else:
                        # 其他 TF：end_ms 锚 → 抓 args.rows 根
                        # before_ms = 最后一根 end_ms + 1（保证包含 end_ms 对应 bar 本身）
                        ohlcv = fetch_okx_history(
                            tf, rows=args.rows,
                            before_ms=int(windows[scene].end_ms + 1),
                        )
                    used_real = True
                except Exception as exc:  # noqa: BLE001
                    if not args.allow_synth:
                        print(
                            f"ERROR: scene={scene} tf={tf} 真实抓取失败且未允许合成：{exc}",
                            file=sys.stderr,
                        )
                        return 3
                    print(f"   WARN  scene={scene} tf={tf} 真实失败 → 合成兜底：{exc}")
                    ohlcv = None

            if ohlcv is None:
                if not args.allow_synth:
                    print(f"ERROR: 合成路径被默认禁用，但真实数据拿不到（scene={scene} tf={tf}）", file=sys.stderr)
                    return 4
                ohlcv = synthesize_scene_tf(scene, tf, args.rows)
                used_synth = True

            write_csv_gz(dst, ohlcv)
            size_kb = dst.stat().st_size / 1024
            c0 = float(ohlcv[0][4]); c1 = float(ohlcv[-1][4])
            pct = (c1 - c0) / max(c0, 1e-12) * 100
            ts0 = time.strftime("%Y-%m-%d", time.gmtime(int(ohlcv[0][0]) / 1000))
            ts1 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ohlcv[-1][0]) / 1000))
            print(f"   ({si}.{ti}) WRITE {fname:<22} {size_kb:>7.1f}KB  rows={len(ohlcv)}  "
                  f"{ts0}→{ts1}  close {c0:.2f}→{c1:.2f} Δ{pct:+.2f}%")

    # =========================================================================
    # B. 最后自检（与 pytest 一致；脚本在 VPS 跑完时一眼能看出数据是否合规）
    # =========================================================================
    print("[3/3] 场景约束快速自检（pytest stage8 会严格重跑）...")
    all_ok = True
    for scene in SCENES:
        # 读 1D CSV.GZ
        path = OUT_DIR / f"{scene}__1d.csv.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            closes = [float(r["close"]) for r in rd]
        c0, c1 = closes[0], closes[-1]
        pct = (c1 - c0) / max(c0, 1e-12)
        amp = (max(closes) - min(closes)) / max(c0, 1e-12)
        ok = {
            "trend_up":   pct >= MIN_TREND_UP_PCT,
            "trend_down": pct <= MIN_TREND_DOWN_PCT,
            "range":      amp <= MAX_RANGE_AMP_PCT,
        }[scene]
        status = "OK " if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"      {status}  scene={scene:<10} 1d close Δ={pct*100:+.2f}%，振幅={amp*100:.2f}%")
    if not all_ok:
        print("ERROR: 场景约束自检失败（pytest 会挂），请检查是否真实数据抓取异常", file=sys.stderr)
        return 5

    summary = [
        f"{'真实 OKX REST' if used_real else '未生成'}",
        f"{' + 确定性合成兜底' if used_synth else ''}",
    ]
    print(f"完成。数据来源：{''.join(summary).strip(' + ')}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
