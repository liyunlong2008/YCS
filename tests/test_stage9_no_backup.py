"""
tests/test_stage9_no_backup.py —— 用户要求「不需要备用 fetch_market_fixtures.py / 合成兜底」，
所以全项目只能有一个拉切片脚本：deploy/pull_real_okx_klines.py，并且不允许任何合成兜底分支。

删完之后再跑此测试应全过。
"""
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent


def run_grep(pattern: str, grep_extra: str = "") -> tuple[int, str]:
    """返回 (exit_code, stdout)，-c 计数模式。"""
    r = subprocess.run(
        f"grep -r -c -E {grep_extra} '{pattern}' "
        f"--include='*.py' --include='*.sh' --include='*.toml' "
        f"{ROOT}/app {ROOT}/deploy {ROOT}/tests {ROOT}/install.sh {ROOT}/pyproject.toml 2>/dev/null",
        shell=True, text=True, capture_output=True,
    )
    return r.returncode, (r.stdout or "").strip()


# ---------- RED 1：fetch_market_fixtures.py 必须不存在 ----------
def test_backup_fetch_script_must_not_exist():
    assert not (ROOT / "deploy" / "fetch_market_fixtures.py").is_file(), (
        "用户明确说「不需要备用」，请删除 deploy/fetch_market_fixtures.py"
    )


# ---------- RED 2：全项目不得出现 "fetch_market_fixtures" 字样 ----------
def test_no_fetch_market_fixtures_string_reference():
    code, out = run_grep("fetch_market_fixtures")
    # grep -c 匹配到非零会输出多行 "file:N"
    hits = [ln for ln in out.splitlines() if not ln.endswith(":0")] if out else []
    # self 文件里的断言字符串本身也要排除（排除本测试文件提及的字符串）
    real_hits = [ln for ln in hits if "test_stage9_no_backup.py" not in ln]
    assert not real_hits, f"仍有 {len(real_hits)} 处引用 fetch_market_fixtures，请全部替换成 pull_real_okx_klines.py 或删除：\n" + "\n".join(real_hits)


# ---------- RED 3：install.sh 必须引用 pull_real_okx_klines，且不得有 YCS_ALLOW_SYNTH 合成兜底 ----------
def test_install_uses_only_pull_real_okx_klines():
    text = (ROOT / "install.sh").read_text()
    assert "pull_real_okx_klines.py" in text, "install.sh 未引用新脚本 deploy/pull_real_okx_klines.py"
    assert "fetch_market_fixtures.py" not in text, "install.sh 仍在引用旧的 fetch_market_fixtures.py"
    assert "YCS_ALLOW_SYNTH" not in text, "用户说「不需要备用」，install.sh 不应再保留合成兜底 YCS_ALLOW_SYNTH"
    assert "--allow-synth" not in text, "install.sh 不应再出现 --allow-synth 参数"


# ---------- RED 4：pyproject.toml 不得再声明 numpy/pandas（只有旧备用脚本用，删脚本后应移除依赖） ----------
def test_pyproject_no_numpy_pandas_dependencies():
    text = (ROOT / "pyproject.toml").read_text()
    assert "numpy" not in text, "pyproject.toml 仍声明 numpy（只在已删除的备用脚本里用，应该移除）"
    assert "pandas" not in text, "pyproject.toml 仍声明 pandas（只在已删除的备用脚本里用，应该移除）"


# ---------- RED 5：load_fixture / stage8 的错误提示也必须是 pull_real_okx_klines.py ----------
@pytest.mark.parametrize("file_rel", [
    "app/storage/fixtures.py",
    "tests/test_stage8_market_fixtures.py",
])
def test_error_messages_use_pull_real_okx_klines(file_rel: str):
    text = (ROOT / file_rel).read_text()
    assert "pull_real_okx_klines.py" in text, f"{file_rel} 错误提示未更新为 pull_real_okx_klines.py"
    assert "fetch_market_fixtures.py" not in text, f"{file_rel} 错误提示仍在用旧脚本名"
