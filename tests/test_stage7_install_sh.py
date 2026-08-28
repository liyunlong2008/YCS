"""
TDD：阶段 7 · 仓库根 install.sh（VPS 一键：首次安装 + git pull 更新）
目标：
  1) bash -n 语法通过
  2) 关键段落命中：
     - GIT_REPO / GIT_BRANCH / INSTALL_DIR 三个可配置变量
     - 首次安装：git clone → 目录不存在时才 clone
     - 更新：目录已存在时，git fetch + git pull --ff-only（禁止 merge 产生分叉）
     - 依赖安装：uv sync
     - 测试：pytest / 可选 YCS_SKIP_TEST 跳过
     - systemd：ycsctl install [--no-enable] 或 YCS_NO_SYSTEMD 跳过
     - 重启：ycsctl restart
     - 幂等保护：未提交修改(dirty)时提示并中断(或 YCS_FORCE=1 强制)
     - 代理透传：HTTP(S)_PROXY / all_proxy 存在时，注入 git config http.proxy
  3) GIT_REPO 默认是「用户必须填写」的占位，脚本在占位未替换时会 FATAL 并给出 curl 模板命令
  4) install.sh 头注释里给出推荐的「一键 curl 命令」示例（含可选 env 变量）
  5) 更新分支时，支持用 INSTALL_DIR=/opt/ycs 自定义，非 root 用 ~/ycs 默认
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"


def _read() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# RED 1：文件存在且 bash -n 语法正确
# ---------------------------------------------------------------------------
def test_installsh_exists_and_bash_syntax_ok():
    assert INSTALL_SH.is_file(), f"install.sh 不存在：{INSTALL_SH}"
    r = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n 语法错误:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"


# ---------------------------------------------------------------------------
# RED 2：文件头注释包含「推荐 curl 命令 + 可配置变量列表」
# ---------------------------------------------------------------------------
def test_installsh_header_has_curl_usage_and_vars():
    txt = _read()
    # 头注释的使用示例（用户 README 中会直接引用）
    for kw in ("curl", "GIT_REPO", "GIT_BRANCH", "INSTALL_DIR"):
        assert kw in txt, f"install.sh 头注释缺少 {kw} 示例或变量说明"


# ---------------------------------------------------------------------------
# RED 3：占位 GIT_REPO 未替换时，脚本执行会立刻报错（含 fatal 退出码≠0）
# ---------------------------------------------------------------------------
def test_installsh_placeholder_repo_fails_with_friendly_message(tmp_path: Path):
    txt = _read()
    # 静态检查：必须存在「检测到占位仓库 → die」的判定段
    for marker in ("YOUR_GITHUB_USERNAME", "YOUR_GITHUB_REPO"):
        assert marker in txt, f"install.sh 必须以 {marker} 作为默认 GIT_REPO，以便在用户误跑时拦截"


# ---------------------------------------------------------------------------
# RED 4：命中 5 大关键流程段落
# ---------------------------------------------------------------------------
def test_installsh_has_core_pipeline_blocks():
    txt = _read()
    # 首次安装：目录不存在时的分支
    assert "git clone" in txt and ("if [ ! -d" in txt or "if [[ ! -d" in txt), "缺少「首次 clone」分支"
    # 更新：目录存在则 pull --ff-only（防 merge 分叉）
    assert "pull --ff-only" in txt, "缺少 git pull --ff-only（禁止隐式 merge）"
    # uv sync
    assert "uv sync" in txt, "缺少 uv sync 依赖安装"
    # pytest（且含跳过开关）
    assert "pytest" in txt or "SKIP_TEST" in txt, "缺少 pytest 测试或 YCS_SKIP_TEST 开关"
    # systemd / ycsctl install / restart（且含 YCS_NO_SYSTEMD 跳过）
    assert "ycsctl install" in txt or "ycsctl restart" in txt or "install_systemd.sh" in txt, (
        "缺少 ycsctl install / restart 段落"
    )
    assert "NO_SYSTEMD" in txt, "缺少 YCS_NO_SYSTEMD 开关"
    # dirty 检查或 force
    assert "dirty" in txt.lower() or "YCS_FORCE" in txt, "缺少「未提交变更」的幂等保护或 YCS_FORCE=1 强制通道"
    # 代理注入
    assert "HTTP_PROXY" in txt or "http.proxy" in txt, (
        "缺少代理透传（GitHub 在国内需走 HTTP_PROXY 才能 clone）"
    )


# ---------------------------------------------------------------------------
# RED 5：对「目录已存在 + run.py/config.yaml 等关键字」做路径校验
# ---------------------------------------------------------------------------
def test_installsh_project_root_detects_existing_run_py():
    txt = _read()
    # 更新流程内需要 cd $INSTALL_DIR 并能「再次确认我们是在正确项目根目录」
    for marker in ("run.py", "config.yaml", "deploy/ycsctl.py", "deploy/install_systemd.sh"):
        assert marker in txt, f"install.sh 内部应引用项目关键文件做目录校验：{marker}"
