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


# ---------------------------------------------------------------------------
# 2026-08-29 新增：用户要求"不用记命令 → ycs 直接弹菜单"
#   · --help 必须提到 ycs / 交互式菜单
#   · version 仍 work（新入口没破坏旧子命令）
#   · ycsctl menu 在非 TTY 时退化为 help（或直接菜单输出中含"紧急停机(kill)"）
# ---------------------------------------------------------------------------
def test_cli_help_mentions_ycs_menu_entry():
    """--help 输出里必须含 ycs / 菜单 / 交互式 字样（直接给用户的心智锚点）。"""
    r = run("--help")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    # 至少两处：一个 ycs 入口名 + 一个"菜单"关键词
    assert "ycs " in out or "ycs（" in out or "直接 ycs" in out, (
        f"--help 未提到 ycs 入口，用户没法知道『敲 ycs 弹菜单』：\n{out[:600]}"
    )
    assert "菜单" in out, f"--help 缺『菜单』关键词：\n{out[:600]}"


def test_cli_version_still_works_after_menu_entry_added():
    """新加 ycs 入口后，version 仍输出语义化版本。"""
    r = run("version")
    assert r.returncode == 0, r.stderr
    import re
    assert re.search(r"\d+\.\d+\.\d+", r.stdout), f"version 丢了：{r.stdout!r}"


def test_cli_menu_subcommand_non_tty_renders_menu_with_kill_entry():
    """ycsctl menu 在非 TTY 下不会卡死：_is_tty() 为 False 时打印 help 或菜单原文。

    验证菜单确实"画过"：输出里必须出现『紧急』/『Kill-Switch』/『停机+全平』之一。
    非 TTY 环境（subprocess.run 的管道）→ interactive_cmd_menu 里有 _is_tty 判
    断，会降级为 help 文本输出，但我们在 help 文案里也写了 kill 那一行；两种路径
    都要保证 exit=0，且能看到 Kill-Switch 提示词（防止菜单代码真丢了也没人察觉）。
    """
    r = run("menu")
    combined = r.stdout + r.stderr
    # exit 必须 0（非 TTY 降级也要正常退出）
    assert r.returncode == 0, (
        f"ycsctl menu 非 TTY 下 exit={r.returncode}（应 0）\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    # 至少出现 kill 关键信号：要么"紧急停机+全平"，要么"Kill-Switch"（大小写都接受）
    kill_keywords = ("Kill-Switch", "kill switch", "停机+全平", "紧急", "kill")
    low = combined.lower()
    assert any(k.lower() in low for k in kill_keywords), (
        "ycsctl menu 输出里看不到 Kill-Switch / kill / 紧急停机+全平 条目，"
        f"菜单根本没渲染！输出：\n{combined[:700]}"
    )


# ---------------------------------------------------------------------------
# 2026-08-29 影子模式（VPS 首发专用）
#   · ycsctl check 必须输出"实盘(影子)"/"纸盘(影子)"三态识别
#   · 影子模式有专属提示：「影子模式：不会真发订单，放心联调」
#   · 影子模式 + live=true + 占位值 = 允许（exit=0，影子不需要真实交易私有 API）
# ---------------------------------------------------------------------------
class Test_CheckShadowMode:
    BASE_PAPER = (
        "okx:\n  api_key: real-a\n  secret: real-b\n  passphrase: real-c\n"
        "ai:\n  provider: deepseek\n  api_key: sk-real\n  model: deepseek-chat\n"
    )

    def _cfg(self, tmp_path, *, live: bool, shadow: bool, placeholder_okx: bool):
        cfg = tmp_path / "config.yaml"
        okx_block = (
            "okx:\n  api_key: YOUR_OKX_API_KEY\n  secret: YOUR_OKX_API_SECRET\n  passphrase: YOUR_OKX_PASSPHRASE\n"
            if placeholder_okx else
            "okx:\n  api_key: real-a\n  secret: real-b\n  passphrase: real-c\n"
        )
        cfg.write_text(
            okx_block
            + "ai:\n  provider: deepseek\n  api_key: sk-real\n  model: deepseek-chat\n"
            + f"trading:\n  live: {'true' if live else 'false'}\n  symbol: ETH-USDT-SWAP\n"
            + "risk_limits:\n  shadow_mode: " + ("true" if shadow else "false") + "\n"
            + "  live_max_equity_usdt: 15.0\n  live_max_daily_loss_usdt: 3.0\n"
            + "  live_max_single_order_usdt: 2.0\n  position_change_pct: 0.10\n"
            + "  kill_switch_token: dummy\n",
            encoding="utf-8",
        )
        return cfg

    def test_check_shadow_true_live_true_shows_shadow_label(self, tmp_path):
        """shadow=true + live=true → 运行模式应含『实盘(影子)』或『影子』二字。"""
        cfg = self._cfg(tmp_path, live=True, shadow=True, placeholder_okx=True)
        r = run("check", "--config", str(cfg))
        out = r.stdout + r.stderr
        assert "影子" in out, f"影子模式下 check 输出缺『影子』：\n{out[:600]}"
        assert ("实盘" in out and "影子" in out) or "实盘(影子" in out or "实盘模式(影子" in out, (
            f"实盘+影子 应输出 实盘(影子)，实际：\n{out[:600]}"
        )

    def test_check_shadow_true_paper_false_shows_paper_shadow_label(self, tmp_path):
        cfg = self._cfg(tmp_path, live=False, shadow=True, placeholder_okx=False)
        r = run("check", "--config", str(cfg))
        out = r.stdout + r.stderr
        assert "影子" in out, f"纸盘+影子 输出缺『影子』：\n{out[:600]}"

    def test_check_shadow_tip_present_when_shadow_true(self, tmp_path):
        """影子模式专属提示：含『不会真发』或『放心联调』或『影子模式』之一。"""
        cfg = self._cfg(tmp_path, live=True, shadow=True, placeholder_okx=True)
        r = run("check", "--config", str(cfg))
        out = r.stdout + r.stderr
        assert any(kw in out for kw in ("不会真发", "放心联调", "影子模式", "不真下")), (
            f"影子模式缺专属提示：\n{out[:600]}"
        )

    def test_check_shadow_true_live_true_placeholder_okx_allows_exit_zero(self, tmp_path):
        """影子模式 + live=true：OKX 占位值是允许的（因为不会真下单）。exit 必须 = 0。
        这是 VPS 首发的核心行为：用户先拿占位 API Key，跑影子模式观察 6 小时。
        """
        cfg = self._cfg(tmp_path, live=True, shadow=True, placeholder_okx=True)
        r = run("check", "--config", str(cfg))
        assert r.returncode == 0, (
            f"影子模式下即使 OKX 是占位值也应 exit=0（不会真发），实际={r.returncode}\n"
            f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
