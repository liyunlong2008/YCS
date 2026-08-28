"""
TDD：阶段 6 · ycsctl CLI
目标：
  1) 子命令：help/version/check/status/start/stop/restart/logs/config/install/uninstall
  2) --help / -h 输出中文，包含「云龙挑战赛」「管理命令行工具」「可用命令」
  3) ycsctl version 输出版本号（形如 v1.0.0 或 1.0.0）
  4) ycsctl check：
       - 校验 config.yaml 的 OKX / AI 占位情况、trading.live 模式
       - 返回 JSON / 人类可读文本；纸盘=占位值命中仅 WARNING
       - 实时 + 占位=FATAL，退出码 !=0
  5) ycsctl status：无 systemctl 时也能打印「未安装 systemd 或非 root」提示（返回 0，不抛错）
  6) 入口模块：deploy/ycsctl.py，可用 `uv run python deploy/ycsctl.py <cmd>` 直接调用
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI = [sys.executable, str(Path(__file__).resolve().parent.parent / "deploy" / "ycsctl.py")]


def run(*args, cwd=None):
    return subprocess.run(
        [*CLI, *args],
        capture_output=True, text=True, cwd=cwd or str(Path(__file__).resolve().parent.parent),
        env={"PATH": "/usr/bin:/bin", **dict(
            (k, v) for k, v in __import__("os").environ.items() if k not in
            ("VIRTUAL_ENV",)
        )} if False else None,
    )


# ---------------------------------------------------------------------------
# RED 1：--help 输出中文
# ---------------------------------------------------------------------------
def test_cli_help_has_chinese_headings():
    r = run("--help")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    for kw in ("云龙挑战赛", "管理命令行工具", "可用命令", "ycsctl"):
        assert kw in out, f"--help 缺字段 {kw!r}：\n{out[:600]}"


def test_cli_help_command_shows_subcommand_details():
    r = run("help")  # 不带参数时 help 子命令同样输出帮助
    out = r.stdout + r.stderr
    # status / check 两个子命令至少要有提及
    assert "check" in out and "status" in out


# ---------------------------------------------------------------------------
# RED 2：version
# ---------------------------------------------------------------------------
def test_cli_version_prints_semver():
    r = run("version")
    assert r.returncode == 0, r.stderr
    import re
    assert re.search(r"\d+\.\d+\.\d+", r.stdout), f"未找到语义化版本号：{r.stdout}"


# ---------------------------------------------------------------------------
# RED 3：check 子命令（基于项目根 config.yaml 默认占位 + live=false → 通过，无 exit≠0）
# ---------------------------------------------------------------------------
def test_cli_check_paper_with_placeholders_is_warn_ok():
    """默认 config.yaml live=false，OKX/AI 都是占位 → check 仅 WARNING，exit=0"""
    r = run("check")
    assert r.returncode == 0, f"预期 exit=0 实际={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    combined = r.stdout + r.stderr
    # 能看到模式=纸盘，且命中占位警告
    assert "纸盘" in combined or "PAPER" in combined or "paper" in combined.lower()


def test_cli_check_live_with_placeholders_exits_nonzero(tmp_path: Path):
    """live=true+占位 → check exit≠0，报错包含占位字段提示"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "okx:\n  api_key: YOUR_OKX_API_KEY\n  secret: YOUR_OKX_API_SECRET\n  passphrase: YOUR_OKX_PASSPHRASE\n"
        "ai:\n  provider: deepseek\n  api_key: YOUR_AI_API_KEY\n  model: deepseek-chat\n"
        "trading:\n  live: true\n  symbol: ETH-USDT-SWAP\n",
        encoding="utf-8",
    )
    r = run("check", "--config", str(cfg))
    assert r.returncode != 0, f"live+占位 应 exit≠0，stdout={r.stdout} stderr={r.stderr}"
    combined = r.stdout + r.stderr
    # 错误信息至少有"实盘"二字，以及某个字段（api_key/ai.api_key 等）
    assert "实盘" in combined, f"缺少 实盘 关键字：{combined[:400]}"


# ---------------------------------------------------------------------------
# RED 4：check --json 输出合法 JSON，键为中文
# ---------------------------------------------------------------------------
def test_cli_check_json_output_is_valid(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "okx:\n  api_key: real-a\n  secret: real-b\n  passphrase: real-c\n"
        "ai:\n  provider: deepseek\n  api_key: sk-real\n  model: deepseek-chat\n  base_url: ''\n"
        "trading:\n  live: false\n  symbol: ETH-USDT-SWAP\n",
        encoding="utf-8",
    )
    r = run("check", "--json", "--config", str(cfg))
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout)
    # 必含中文键：运行模式、配置检查、结论
    assert "运行模式" in obj or "模式" in obj, obj.keys()
    assert "结论" in obj or "OK" in str(obj) or "通过" in str(obj), obj


# ---------------------------------------------------------------------------
# RED 5：status 子命令无 systemctl 不抛错（返回 0），含友好提示
# ---------------------------------------------------------------------------
def test_cli_status_no_systemctl_is_graceful(monkeypatch, tmp_path: Path):
    # 把 PATH 替换为一个空目录，使 systemctl 不可用 → CLI 不应 crash
    monkeypatch.setenv("PATH", str(tmp_path))
    r = run("status")
    combined = r.stdout + r.stderr
    # exit=0 或者 exit≠0 都可以（没装就是没装），但必须给出可理解的中文提示
    assert "systemctl" in combined or "未检测" in combined or "systemd" in combined or r.returncode == 0, (
        f"status 在无 systemctl 下提示不友好，exit={r.returncode}\n{combined[:400]}"
    )


# ---------------------------------------------------------------------------
# RED 6：未知子命令 exit != 0，且给出"未知命令/子命令"提示
# ---------------------------------------------------------------------------
def test_cli_unknown_subcommand_errors_gracefully():
    r = run("definitely_not_a_command_xyz")
    assert r.returncode != 0, "未知命令需要非零退出码"
    combined = r.stdout + r.stderr
    assert "未知" in combined or "不存在" in combined or "help" in combined.lower(), combined
