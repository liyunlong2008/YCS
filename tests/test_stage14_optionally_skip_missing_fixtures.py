"""TDD stage14: 远端 f60f3ac 删掉 18 个 csv.gz 后，pytest 必须仍全绿（不要 25 FAIL）。

策略：
  · 只要 18 个文件不都存在，stage8 的 fixture 测试全部 pytest.skip（不改断言语义，
    有文件时仍强约束 1200 行/涨跌幅/振幅）
  · stage10 /api/diag fixtures.file_count：断言支持"二分状态"
      → 若 18 个文件在 → file_count==18
      → 若 18 个文件不在 → file_count==0 且 fixtures.hint 字符串包含
        'pull_real_okx_klines'（提示用户如何生成，而不是断言炸掉）
  · stage12 sources_sum==file_count：同样支持 0 根时 0==0 通过，18 根时 18==18 通过
  · 新增 conftest fixtures_available(session) 缓存判定 + pytest.mark.needs_fixtures 自动 skip
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


FIXTURES_ROOT = REPO / "tests" / "fixtures" / "market_data"
SCENES = ("trend_up", "trend_down", "range")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")


def _all_18_exist() -> bool:
    for s in SCENES:
        for tf in TIMEFRAMES:
            if not (FIXTURES_ROOT / f"{s}__{tf}.csv.gz").is_file():
                return False
    return True


class Test_A_Stage8_SkipsWithoutFixtures:
    def test_stage8_has_needs_fixtures_marker_or_auto_skips(self):
        """若 18 个 fixture 文件不存在，运行 stage8 时应产生 N SKIPPED，绝不能出现 FAIL。
        FAIL 数字 = 0。"""
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header",
             "tests/test_stage8_market_fixtures.py"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        tail = (r.stdout or "") + "\n" + (r.stderr or "")
        # 在"无 fixture"的情况下：退出码 0 或 5（no tests collected）都可接受；
        # 但不能 1（FAIL）。有 fixture 的情况下当然也要 0 退出，但当前沙箱是无 fixture。
        if _all_18_exist():
            assert r.returncode == 0, f"有 fixture 但 stage8 返回 {r.returncode}：\n{tail[-800:]}"
        else:
            # 必须没有 FAILED
            assert "FAILED" not in tail, (
                f"远端已删 18 个 fixture，但 stage8 仍 FAIL（应当 SKIP/无 collected）。"
                f"\n退出码={r.returncode}\n末尾输出:\n{tail[-1000:]}"
            )
            # 退出码必须 ∈ {0, 5}（0=全 skip 或空；5=no tests collected → 但 skip 是 0）
            assert r.returncode in (0, 5), f"退出码 {r.returncode}：\n{tail[-600:]}"


class Test_B_Stage10_Diag_FixtureCount_Optional:
    def test_stage10_diag_fixture_count_handles_missing_without_fail(self):
        """stage10 的 test_diag_fixtures_section_contains_stage9_and_stage8_status 不能再因为
        file_count 不是 18 就 FAIL；支持 0 / 18 两种合法状态，0 时 hint 含 pull_real_okx_klines
        字样（方便用户修复）。"""
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "--no-header", "--tb=short",
             "tests/test_stage10_risk_controls_and_diag.py::Test_B_DiagnosticSnapshotAPI::"
             "test_diag_fixtures_section_contains_stage9_and_stage8_status"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        tail = r.stdout + "\n" + r.stderr
        assert "FAILED" not in tail, (
            f"stage10 fixtures.file_count 在缺文件时应二分通过，当前 FAIL。\n"
            f"退出码={r.returncode}\n{tail[-1500:]}"
        )
        assert r.returncode == 0


class Test_C_Stage12_SumEqFileCount_Binary:
    def test_stage12_fixtures_sources_eq_filecount_works_with_zero_or_eighteen(self):
        """stage12 Test_4_FixtureSourcesPerFile.test_diag_fixtures_sources_sum_matches_file_count
        不能再因 0 != 18 FAIL；0 文件时 sources_sum=0 通过，18 时 18 通过。"""
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "--no-header", "--tb=short",
             "tests/test_stage12_diag_bugfixes.py::Test_4_FixtureSourcesPerFile::"
             "test_diag_fixtures_sources_sum_matches_file_count"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        tail = r.stdout + "\n" + r.stderr
        assert "FAILED" not in tail, (
            f"stage12 sources_sum 在 0 文件时必须过。退出码={r.returncode}\n{tail[-1500:]}"
        )
        assert r.returncode == 0
