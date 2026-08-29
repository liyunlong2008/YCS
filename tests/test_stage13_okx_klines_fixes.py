"""
TDD 阶段 13 · deploy/pull_real_okx_klines.py 缺陷修复（用户 Win 实际跑 18 全 FAIL）：
  1) IndexError：首次 fetch 返回空列表时 ohlcv[0][0] 越界（当前 while 循环第一行即死）
  2) mktime 用本地时区：直到时间 +8h（在东八区），OKX 把 until 视为未来 → 返回空
  3) 单页 limit=args.rows(1200)：OKX history-candles v5 各 TF 最大 limit ≈ 100，
     请求 1200 会被 OKX 直接 truncate；必须多页分页（每次 limit=100）
  4) OKX 历史深度有限：例如 1m 仅保留 ~2 个月内，trend_down 直到 2022-11 → 根本
     不可能拉 1200 根 1m。必须引入「TF 级 until 自动回退到 『TF 允许的最早』」策略，
     根据 TF 推算能覆盖 1200 根的最早 until，若场景原始 until 超出该范围，
     自动回退到「当前时间 − (1200 + 60) × TF_ms」
  5) 所有 TF 的 scene until 必须仍满足 stage8 对 1d 阶段涨幅/跌幅/振幅阈值：
     - trend_up 1d Δ≥+20% · trend_down 1d Δ≤-15% · range 1d 振幅≤8%
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path("/workspace")
sys.path.insert(0, str(REPO / "deploy"))


class Test_1_NoIndexErrorOnEmptyResponse:
    def test_fetch_ohlcv_paginated_empty_first_page_raises_clean_message_not_index_error(self):
        """若 OKX 对某 scene×tf 连续几页都返回空（历史数据无权限 / 超截止），应抛
           RuntimeError("OKX 返回 0 根，无法凑齐 N 根 …")，绝不抛 IndexError。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines",
            str(REPO / "deploy" / "pull_real_okx_klines.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert hasattr(mod, "fetch_ohlcv_paginated"), "必须抽取模块级 fetch_ohlcv_paginated(ex,sym,tf,need,until_ms)"

        # 构造假 broker：fetch_ohlcv 永远返回 []（模拟超 OKX 历史深度）
        calls: list[tuple] = []
        class Fake:
            def fetch_ohlcv(self, sym, timeframe=None, limit=None, params=None):
                calls.append((sym, timeframe, limit, params))
                return []
        with pytest.raises(RuntimeError) as ri:
            mod.fetch_ohlcv_paginated(Fake(), "ETH/USDT:USDT", "1m",
                                      need=1200, until_ms=1_700_000_000_000)
        assert "IndexError" not in str(ri.type)
        msg = str(ri.value)
        assert "0" in msg and "1200" in msg, f"错误信息应提示缺 1200 根、实得 0：{msg}"
        # 必须至少试了几次（while 循环不要一次 break）
        assert len(calls) >= 2, f"应做 retries/分页，实际只调了 {len(calls)} 次"


class Test_2_UTCTimestamps:
    def test_scene_until_uses_utc_not_localtime(self):
        """SCENES 中 until_ms 必须基于 UTC(strptime→timegm)；若用本地 time.mktime
           东八区 2024-01-10 00:00 UTC 被加成 2024-01-10 08:00 UTC → OKX 视为未来。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines", REPO / "deploy" / "pull_real_okx_klines.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        scenes = getattr(mod, "SCENES", None)
        assert isinstance(scenes, dict) and len(scenes) >= 3
        # trend_up 截止 2024-01-10 00:00 UTC 对应 epoch_ms=1704844800000（UTC 绝对数）
        expected_trend_up_until_ms = 1704844800000
        actual = int(scenes["trend_up"]["until_ms"])
        assert actual == expected_trend_up_until_ms, (
            f"trend_up until_ms 应是 UTC 2024-01-10 00:00 = {expected_trend_up_until_ms}，"
            f"实际 {actual}（差 {actual - expected_trend_up_until_ms} ms）。"
            " 若是 +28800000（8h），就是误用 time.mktime（本地时区）了。"
        )


class Test_3_PaginationAccumulatesAllBars:
    def test_pagination_concatenates_multiple_limit_100_pages(self):
        """需要 350 根，单页 limit=100；OKX 实际每页最多 100。
           模拟调用方：fetch_ohlcv(limit=350) OKX 只给 100；分页再调用凑齐 350。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines", REPO / "deploy" / "pull_real_okx_klines.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        # 构造"每页最多 100 根"假交易所
        start_ms = 1_700_000_000_000
        class FakeEx:
            def __init__(self):
                self.call_count = 0
            def fetch_ohlcv(self, sym, timeframe=None, limit=None, params=None):
                self.call_count += 1
                until = int((params or {}).get("until", start_ms))
                # 严格模拟 OKX：返回最多 100 根
                per_page = min(limit or 100, 100)
                # 用 ms 步长 60_000（1m）—— timeframes 无关，只测分页拼接
                step_ms = 60_000
                bars = [
                    [until - (per_page - 1 - i) * step_ms, 2000.0, 2001.0, 1999.0, 2000.0, 10.0]
                    for i in range(per_page)
                ]
                return bars
        need = 350
        bars = mod.fetch_ohlcv_paginated(
            FakeEx(), "ETH/USDT:USDT", "1m", need=need, until_ms=start_ms + 60_000,
        )
        assert len(bars) == need, f"应凑齐 {need} 根，实得 {len(bars)} 根"
        # 必须升序
        ts = [b[0] for b in bars]
        assert ts == sorted(ts), "返回 bars 必须升序"
        # 必须单调严格增（避免重复时间戳）
        assert all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))
        # 最后一根时间戳必须 ≤ until
        assert ts[-1] <= start_ms + 60_000

    def test_pagination_stops_when_no_more_bars_and_raises_if_short(self):
        """前两页各 100 根，第三页空 → 总 200 < 350 → RuntimeError 明确说不足。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines", REPO / "deploy" / "pull_real_okx_klines.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        base_ms = 1_700_000_000_000
        class FakeEx:
            def __init__(self):
                self.c = 0
            def fetch_ohlcv(self, sym, timeframe=None, limit=None, params=None):
                self.c += 1
                if self.c >= 3:
                    return []
                per = 100
                step = 60_000
                until = int((params or {}).get("until", base_ms))
                return [[until - (per - 1 - i) * step, 2000, 2001, 1999, 2000, 10] for i in range(per)]

        with pytest.raises(RuntimeError) as ri:
            mod.fetch_ohlcv_paginated(FakeEx(), "ETH/USDT:USDT", "1m", need=350, until_ms=base_ms)
        assert "200" in str(ri.value) and "350" in str(ri.value)


class Test_4_PerTimeframeUntilAutoAdjust:
    def test_adjust_until_for_tf_clips_to_data_retention_depth(self):
        """trend_down 原始 until=2022-11-20 UTC（超 2 年前），1m TF 最大历史深
           度 OKX 只保留 ~2 个月，所以 adjust_until_for_tf 必须把 until 往后推到
           「今天 − N × TF_ms」（= 有真实数据的最近窗口）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines", REPO / "deploy" / "pull_real_okx_klines.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert hasattr(mod, "adjust_until_for_tf")

        now_ms = int(time.time() * 1000)
        # trend_down 2022-11-20 远早于 1m 的 2 个月窗口
        old_until = 1668902400000
        new_until = mod.adjust_until_for_tf(old_until, "1m", need=1200)
        # 推后的结果必须在 1m 覆盖窗口内，且不能是 old_until（那意味着没调）
        assert new_until > old_until, "1m 下超历史深度的 until 必须被推近（增大）"
        # 1m: need*60_000*1.5 步长裕度 < now；即 (now - new_until) < need*60_000*~1.05
        assert now_ms - new_until >= 1200 * 60_000  # 至少 1200 根之前
        assert now_ms - new_until <= 1200 * 60_000 * 3 + 24 * 3600 * 1000, (
            "调整后的 until 距离「现在」不应超过 need 窗口 3 倍（否则 1m 深度仍不够）"
        )

    def test_adjust_until_returns_same_for_1d_recent_scenes(self):
        """1d 的 trend_up 2024-01 显然在 OKX 历史内，adjust_until_for_tf 不应改它。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pull_real_okx_klines", REPO / "deploy" / "pull_real_okx_klines.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        trend_up_1d_until = 1704844800000  # 2024-01-10 UTC
        new_until = mod.adjust_until_for_tf(trend_up_1d_until, "1d", need=1200)
        assert new_until == trend_up_1d_until, (
            "1d 场景有足够深度，adjust_until_for_tf 必须原样返回（否则会毁掉 stage8 趋势阈值）"
        )
