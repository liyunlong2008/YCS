"""conftest.py: 全局 session-scoped fixture『fixtures_available』+ 自动 skip 机制。

为什么要有：
  · 远端 f60f3ac 已删除 tests/fixtures/market_data/*.csv.gz（18 个），让仓库
    不含大体积二进制；用户本机跑 deploy/pull_real_okx_klines.py 生成本地未跟踪文件
    就能过 stage8（含 1200 行/涨跌幅/振幅阈值）、stage9（不需要文件但 stage10/12
    做综合自检仍要语义清晰）。
  · 缺 18 个文件时：对 *stage8* 和任何 needs_fixtures 的测试 → 统一 SKIP，
    不产生 FAIL（让 pytest 退出码仍是 0）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "market_data"
ALL_SCENES = ("trend_up", "trend_down", "range")
ALL_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
EXPECTED_FIXTURE_COUNT = len(ALL_SCENES) * len(ALL_TIMEFRAMES)  # = 18


def _all_fixtures_present(root: Path = FIXTURES_ROOT) -> bool:
    for s in ALL_SCENES:
        for tf in ALL_TIMEFRAMES:
            if not (root / f"{s}__{tf}.csv.gz").is_file():
                return False
    return True


@pytest.fixture(scope="session")
def fixtures_available() -> bool:
    return _all_fixtures_present()


@pytest.fixture(scope="session")
def fixtures_root() -> Path:
    return FIXTURES_ROOT


def pytest_collection_modifyitems(config, items):  # noqa: ARG001 - pytest hook 固定签名
    """在收集阶段给缺少 fixture 的 stage8 自动打 skip，避免远端删文件后整组 FAIL。

    规则：
      1. 任意 item 若模块名包含 'stage8_market_fixtures' 且 18 文件不全 → skip
      2. 任意 item 若被显式 @pytest.mark.needs_fixtures 标注且 18 文件不全 → skip
    """
    available = _all_fixtures_present()
    if available:
        return
    reason = (
        "远端/本地仓库未包含 18 个真实 OKX fixture 文件（tests/fixtures/market_data/*.csv.gz）。"
        "本地先执行：uv run python deploy/pull_real_okx_klines.py（需要代理 127.0.0.1:10808）。"
        "生成后 re-run pytest 即可启用 stage8 强断言（1200 行/涨跌幅/振幅）。"
    )
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        is_stage8 = "stage8_market_fixtures" in item.module.__name__ if getattr(item, "module", None) else False
        has_marker = item.get_closest_marker("needs_fixtures") is not None
        if is_stage8 or has_marker:
            item.add_marker(skip_marker)
