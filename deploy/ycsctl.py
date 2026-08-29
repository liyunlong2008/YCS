#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# 云龙挑战赛（YCS）管理命令行工具 · ycsctl
# 用法：
#   uv run python deploy/ycsctl.py help          # 帮助
#   uv run python deploy/ycsctl.py version       # 版本号
#   uv run python deploy/ycsctl.py check         # 配置自检（中文输出 + 非零退出码区分致命/警告）
#   uv run python deploy/ycsctl.py status        # 服务状态（调用 systemctl status ycs）
#   uv run python deploy/ycsctl.py start         # 启动服务（systemctl start ycs）
#   uv run python deploy/ycsctl.py stop          # 停止服务
#   uv run python deploy/ycsctl.py restart       # 重启服务
#   uv run python deploy/ycsctl.py logs [-n 200] [-f]   # journalctl 日志
#   uv run python deploy/ycsctl.py config [show|path]   # 查看/定位 config.yaml
#   uv run python deploy/ycsctl.py install [--no-enable] # 调用 install_systemd.sh
#   uv run python deploy/ycsctl.py uninstall     # 调用 install_systemd.sh --uninstall
# =============================================================================
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---- 常量（单一事实来源）----
__version__ = "1.0.0"
UNIT_NAME = "ycs"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.yaml.example"
INSTALL_SCRIPT = PROJECT_ROOT / "deploy" / "install_systemd.sh"
APP_NAME_CN = "云龙挑战赛（YCS）"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _systemctl(*args: str, sudo: bool = True) -> subprocess.CompletedProcess[str]:
    prefix = ["sudo"] if sudo and os.geteuid() != 0 else []  # type: ignore[attr-defined]
    return subprocess.run(
        [*prefix, "systemctl", *args], capture_output=True, text=True,
    )


def _journalctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["journalctl", "-u", UNIT_NAME, *args], capture_output=False,
    )


def _ensure_config_from_example(cfg_path: Path) -> bool:
    """若 cfg_path（一般 config.yaml）不存在，自动从同目录的 config.yaml.example 复制。

    返回：
        True  本函数刚创建（从 example 复制成功）
        False 已存在（不覆盖），或 example 也不存在 / 复制失败
    """
    if cfg_path.is_file():
        return False
    example = cfg_path.with_name(cfg_path.name + ".example")
    if not example.is_file():
        # 兜底：如果不是 config.yaml 名字，也尝试默认 project example 常量
        fallback = DEFAULT_EXAMPLE_CONFIG_PATH
        if fallback.is_file():
            example = fallback
        else:
            return False
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, cfg_path)
    except Exception:
        return False
    # 复制后权限 600（含密钥，最小权限）
    try:
        cfg_path.chmod(0o600)
    except Exception:
        pass
    return True


def _print_err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_help() -> int:
    print(f"""{APP_NAME_CN} · 管理命令行工具 v{__version__}
用法：
  ycs                     直接呼出【交互式菜单】（不记命令时首选）
  ycsctl <命令> [选项]    单命令模式（脚本/CI 友好，等价原 ycsctl）
  ycsctl menu             同 ycs：呼出交互式菜单

可用命令：
  help, -h, --help       显示本帮助
  menu                   【推荐】呼出数字选项交互式菜单（不用记命令）
  version                显示版本号
  check [--json] [--config PATH]   配置自检：密钥占位 / 运行模式 / 依赖
  status                 查询服务运行状态（systemctl status ycs）
  start                  启动服务（需 root / sudo）
  stop                   停止服务
  restart                重启服务（修改配置后执行）
  logs [-n N] [-f]       打印 / 跟随日志（journalctl -u ycs）
  config [show|path]     显示 config.yaml 内容 / 仅打印路径
  install [--no-enable]  一键安装 systemd 服务（自动调用 install_systemd.sh）
  uninstall              停止 + 禁用 + 删除 unit（需 root / sudo）
  kill [--token T] [--host URL]  【紧急】一键紧急停机+全平所有仓位（调 /api/kill → EMERGENCY_HALT → systemctl stop 三通道）

提示：
  · 所有子命令优先在 /workspace 项目根目录查找配置与脚本
  · install / start / stop / restart 需要 root（自动 sudo 交互，若 sudo 需要密码时请前置 sudo）
  · 非 root 容器（未装 systemd）下 start/stop/restart 会给出友好提示
  · 忘命令 → 直接敲 ycs 回车走菜单
""")
    return 0


# ---------------------------------------------------------------------------
# 交互式菜单（ycs / ycsctl menu / ycsctl 无参数 → 都进这里）
# ---------------------------------------------------------------------------
def _c_red(s: str) -> str:   return f"\033[1;31m{s}\033[0m"
def _c_green(s: str) -> str: return f"\033[1;32m{s}\033[0m"
def _c_yellow(s: str) -> str: return f"\033[1;33m{s}\033[0m"
def _c_bold(s: str) -> str:  return f"\033[1m{s}\033[0m"
def _c_dim(s: str) -> str:   return f"\033[2m{s}\033[0m"


def _is_tty() -> bool:
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty() and sys.stdout.isatty()


# 菜单项：(key, emoji+标题, 帮助子提示, 执行函数返回 argv list)
# 说明：返回 argv 给 main() 递归调用，保持"单命令模式 / 菜单模式"同一套命令执行链路，
#      避免 kill/check/install 等逻辑重复实现两遍。
def _build_menu() -> list[tuple[str, str, str, list[str] | None]]:
    return [
        # A. 启动/停止/状态
        ("1",  "🚀 启动 ycs 服务 (start)",              "systemctl start ycs（需要 sudo）",        ["start"]),
        ("2",  "⏹ 停止 ycs 服务 (stop)",               "systemctl stop ycs（需要 sudo）",         ["stop"]),
        ("3",  "🔄 重启 ycs 服务 (restart)",           "修改 config.yaml 后必须先重启",           ["restart"]),
        ("4",  "💡 查询服务状态 (status)",              "systemctl status ycs + 主进程/端口速览",  ["status"]),
        ("",   None, None, None),
        # B. 诊断 / 日志 / 配置
        ("5",  "🛡 配置自检 (check)",                   "输出 纸盘/实盘 + 占位密钥警告 + 退出码区分致命",  ["check"]),
        ("6",  "📜 最近 200 行日志 (logs -n 200)",      "journalctl -u ycs -n 200",                ["logs", "-n", "200"]),
        ("7",  "👁 实时跟随日志 (logs -f)",             "Ctrl+C 退出（journalctl -f）",            ["logs", "-f"]),
        ("8",  "📂 查看 config.yaml (config show)",     "打印配置内容；config path 只显示文件路径", ["config", "show"]),
        ("",   None, None, None),
        # C. 更新/安装
        ("9",  "⬆️ VPS 更新代码 + 重启 (restart)",      "需要先跑 install.sh，这里是改完 config/代码后重启", ["restart"]),
        ("a",  "📦 安装 systemd 服务 (install)",       "调用 deploy/install_systemd.sh（首次部署）", ["install"]),
        ("b",  "🗑 卸载 systemd 服务 (uninstall)",      "停止+禁用+删除 unit（需要 sudo）",         ["uninstall"]),
        ("",   None, None, None),
        # D. 紧急 & 帮助（顶部红条）
        (_c_red("k"), _c_red("🔴【紧急】一键 Kill-Switch 停机+全平 (kill)"),
                "三通道：/api/kill → EMERGENCY_HALT → systemctl stop",     ["kill"]),
        ("h",  "❔ 显示完整帮助 (help)",               "所有子命令 + 选项详解",                    ["help"]),
        ("v",  "ℹ️ 版本号 (version)",                  __version__,                              ["version"]),
        ("",   None, None, None),
        ("q",  "✖️ 退出菜单",                         "退出（Ctrl+C / Ctrl+D 也可以）",           None),
    ]


def interactive_cmd_menu() -> int:
    """交互式 TUI 菜单：用户直接敲『ycs』或『ycsctl menu』就进这里。

    特性：
      · 输入数字/字母 → 对应子命令通过 main([argv]) 同一链路执行，保证菜单与命令行等价
      · 执行完一次子命令后回到主菜单（方便反复 status/logs）
      · 支持 Ctrl+C / Ctrl+D / q 退出
      · 非 TTY 时（例如有人用 ycsctl < input.txt 或 | 管道）自动降级为 help，避免卡死
    """
    if not _is_tty():
        print("[info] 非交互环境（stdin/stdout 非 TTY），菜单模式降级为直接打印 help。")
        print("[hint] 走单命令：ycsctl check / ycsctl status / ycsctl kill …")
        return cmd_help()

    # 清屏（兼容 Windows/Unix）
    def _clear() -> None:
        sys.stdout.write("\033[2J\033[H" if os.name != "nt" else "\x1bc")
        sys.stdout.flush()

    last_cmd_rc: int | None = None
    last_cmd_summary: str = ""
    while True:
        try:
            _clear()
            # 顶部标题条
            title = (
                f"{_c_bold(APP_NAME_CN)} · {_c_bold('交互式菜单')}   "
                f"{_c_dim(f'v{__version__}  项目根: {PROJECT_ROOT}')}"
            )
            print(title)
            print(_c_dim("─" * max(80, len(title) - 20)))
            if last_cmd_summary:
                color = _c_green if last_cmd_rc == 0 else _c_red
                print(f"  {color('上次执行：' + last_cmd_summary)}  →  exit={last_cmd_rc}")
                print(_c_dim("─" * 80))

            # 分组打印
            print(_c_yellow("  A. 服务管理") + _c_dim("        (start/stop/restart/status)"))
            for k, title, hint, _ in _build_menu()[:5]:
                if title is None:
                    print()
                    print(_c_yellow("  B. 诊断 & 日志") + _c_dim("       (check/logs/config)"))
                    continue
                if title and (k in "1234"):
                    pass
                if title and k in ("5","6","7","8"):
                    pass
                if title is None:
                    continue
                # 不按 A/B/C 分段了，直接用空行切
            # —— 上面的 for 仅为"空行+分组标题"打印占位；下面统一全表打印
            print()
            print(_c_yellow("  ┌──────────────────────────────────────────────────────────────────────┐"))
            idx_map: dict[str, list[str] | None] = {}
            current_group = ""
            rows = _build_menu()
            for k, title, hint, target_argv in rows:
                if title is None:
                    # 空行分隔
                    print("  ├──────────────────────────────────────────────────────────────────────┤")
                    continue
                idx_map[k] = target_argv
                kk = _c_red(k) if k.lower() == "k" else _c_green(k.rjust(2))
                right = _c_dim(f"[{hint}]") if hint else ""
                print(f"  │ {kk}. {title:<58} {right} │")
            print("  └──────────────────────────────────────────────────────────────────────┘")
            print()

            prompt_header = (
                _c_bold("  请选择编号/字母 ")
                + _c_dim("(q 退出；回车重刷菜单；输入对应字母/数字后回车)")
                + _c_bold(" > ")
            )
            try:
                choice = input(prompt_header).strip()
            except EOFError:
                print()
                print("👋 再见（Ctrl+D）")
                return 0

            if choice in ("", "\n"):
                # 刷新
                continue
            if choice.lower() in ("q", "quit", "exit", "x"):
                print("👋 再见")
                return 0

            if choice not in idx_map:
                last_cmd_rc = 2
                last_cmd_summary = f"输入 {choice!r} 不在选项里（请输入菜单左列的数字/字母，如 1/5/k/q）"
                continue

            target_argv = idx_map[choice]
            if target_argv is None:
                # q 退出分支（已在上文处理过；这里以防 idx_map["q"]=None 兜底）
                print("👋 再见")
                return 0

            # 紧急 kill 二次确认
            if len(target_argv) >= 1 and target_argv[0] == "kill":
                confirm = input(
                    _c_red("  ⚠️ 即将执行 kill：撤所有挂单+市价全平+熔断24h，输入 YES 继续 > ")
                ).strip()
                if confirm != "YES":
                    last_cmd_rc = 1
                    last_cmd_summary = "KILL-SWITCH 已取消（未输入 YES）"
                    continue
                last_cmd_summary = "执行 ycsctl kill（紧急停机+全平）"
            elif len(target_argv) >= 1 and target_argv[0] == "install":
                confirm = input(
                    _c_yellow("  ⚠️  即将执行 install：部署/覆盖 systemd unit，输入 y 继续 > ")
                ).strip()
                if confirm.lower() not in ("y", "yes"):
                    last_cmd_rc = 1
                    last_cmd_summary = "install 已取消"
                    continue
                last_cmd_summary = "执行 ycsctl install（部署 systemd 服务）"
            elif len(target_argv) >= 1 and target_argv[0] == "uninstall":
                confirm = input(
                    _c_red("  ⚠️  即将执行 uninstall：停止+卸载 systemd unit，输入 YES 继续 > ")
                ).strip()
                if confirm != "YES":
                    last_cmd_rc = 1
                    last_cmd_summary = "uninstall 已取消"
                    continue
                last_cmd_summary = "执行 ycsctl uninstall（卸载 systemd 服务）"
            else:
                last_cmd_summary = f"执行 ycsctl {' '.join(target_argv)}"

            print(_c_dim("─" * 80))
            print(_c_bold(f"  → $ ycsctl {' '.join(target_argv)}"))
            print(_c_dim("─" * 80))
            # 递归调用 main：复用 argparse + 同一套 cmd_* 实现
            try:
                rc = main(target_argv)
            except SystemExit as se:
                rc = int(se.code) if se.code is not None else 0
            last_cmd_rc = rc
            print()
            # 执行完后等一下，让用户看输出
            wait = input(_c_dim("  回车返回菜单 / q 退出 > ")).strip()
            if wait.lower() in ("q", "quit", "exit"):
                print("👋 再见")
                return 0
        except KeyboardInterrupt:
            print()
            print("👋 再见（Ctrl+C）")
            return 0


def cmd_version() -> int:
    print(__version__)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import sys as _sys
    # 允许 --config 指定
    cfg_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH

    mode_cn = "未知"
    live = False
    shadow = False  # A7. 影子模式（总开关）
    problems: list[str] = []
    warns: list[str] = []
    oks: list[str] = []

    # ---- 0. 配置文件存在性：缺失时从 config.yaml.example 自动创建（同 install.sh 行为）----
    if not cfg_path.is_file():
        created = _ensure_config_from_example(cfg_path)
        if created:
            warns.append(
                f"未找到 config.yaml → 已自动从同目录 config.yaml.example 复制生成（{cfg_path}）。"
                " 编辑该文件填入真实 OKX / AI 凭证后再切实盘。"
            )
        else:
            problems.append(f"配置文件不存在且未找到 config.yaml.example 模板：{cfg_path}")
            mode_cn = "未知（无配置）"
            # 继续往下输出报告；下面 raw.get 不会读到值
            raw: dict[str, Any] = {}
    if cfg_path.is_file():
        # ---- 1. 解析 YAML ----
        try:
            import yaml  # type: ignore
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            problems.append(f"config.yaml 解析失败（{type(exc).__name__}）：{exc}")
            raw = {}

        okx = raw.get("okx") or {}
        ai = raw.get("ai") or {}
        trading = raw.get("trading") or {}
        risk_limits = raw.get("risk_limits") or {}
        live = bool(trading.get("live", False))
        shadow = bool(risk_limits.get("shadow_mode", False))
        # 四态：live × shadow → 纸盘 / 纸盘(影子 SHADOW) / 实盘 / 实盘(影子 SHADOW)
        mode_cn = "实盘" if live else "纸盘"
        if shadow:
            mode_cn = f"{mode_cn}(影子 SHADOW)"

        # ---- 2. 占位值判定 + 影子模式专属放行
        def _is_placeholder(s: str) -> bool:
            s = (s or "").strip()
            if not s:
                return True
            if s.upper().startswith("YOUR_"):
                return True
            if s.lower() in {"xxx", "todo", "changeme", "placeholder"}:
                return True
            return False

        checks = {
            "okx.api_key":    str(okx.get("api_key", "") or ""),
            "okx.secret":     str(okx.get("secret", "") or ""),
            "okx.passphrase": str(okx.get("passphrase", "") or ""),
            "ai.api_key":     str(ai.get("api_key", "") or ""),
        }
        placeholder_keys = [n for n, v in checks.items() if _is_placeholder(v)]

        # 影子模式 + live=true：OKX 占位值仅 WARN，不 FATAL（不真发单）
        if placeholder_keys:
            # OKX 类占位 → shadow 下都只 WARN
            okx_placeholders = [n for n in placeholder_keys if n.startswith("okx.")]
            # AI 类占位 → 任何模式都是 WARN（AI 有离线兜底模型）
            ai_placeholders = [n for n in placeholder_keys if n.startswith("ai.")]

            if live and not shadow:
                # 真正实盘：OKX 占位 → FATAL；AI 占位只 WARN
                if okx_placeholders:
                    problems.append(
                        "实盘模式（无影子）下 OKX 密钥仍是占位值，绝不允许真发单："
                        + ", ".join(okx_placeholders)
                    )
                if ai_placeholders:
                    warns.append(f"[离线兜底可接受] 占位 AI 密钥：{', '.join(ai_placeholders)}")
            else:
                # 纸盘 或 影子：所有占位仅 WARN
                if shadow and okx_placeholders:
                    warns.append(
                        "[影子模式 OK · 不会真下] OKX 占位值："
                        + ", ".join(okx_placeholders)
                        + "（如需观察真实账户/行情，请填真实只读密钥）"
                    )
                elif okx_placeholders:
                    warns.append(f"[纸盘可接受] 占位：{', '.join(okx_placeholders)}")
                if ai_placeholders:
                    warns.append(f"[离线兜底可接受] 占位 AI 密钥：{', '.join(ai_placeholders)}")

        # ---- 可选：尝试用 validate_runtime_credentials 增强（当前 v1 不依赖，兼容保留）----
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from app.core.safety import validate_runtime_credentials  # type: ignore
        except Exception:
            validate_runtime_credentials = None  # type: ignore

        if validate_runtime_credentials is not None and not shadow and live:
            # 只有真正"无影子的实盘"才走严格 FATAL 闸门；影子/纸盘 走上面轻量判定
            try:
                ok_flag = validate_runtime_credentials(
                    live=True,
                    okx_api_key=checks["okx.api_key"],
                    okx_secret=checks["okx.secret"],
                    okx_passphrase=checks["okx.passphrase"],
                    ai_api_key=checks["ai.api_key"],
                )
                if ok_flag and not placeholder_keys:
                    oks.append("安全自检通过（实盘非影子严格模式：无占位）")
            except RuntimeError as exc:
                # 双重保险：理论上前面占位 FATAL 已进 problems，这里重复捕获也 OK
                problems.append(f"安全自检拦截（实盘）：{exc}")
        elif not placeholder_keys:
            oks.append("密钥自检通过（无占位值）")

        # ---- 影子模式专属提示（强制加一条 OK / 提示行，保证一眼可见）----
        if shadow:
            oks.append("影子模式：已开启，下单/撤单会被记录但不会真发到交易所，放心联调")

        # ---- 3. 其它依赖检查 ----
        for dep, hint in (
            ("uv", "部署前请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"),
        ):
            if _have(dep):
                oks.append(f"依赖 {dep} 已就绪")
            else:
                warns.append(f"未检测到 {dep}（{hint}）")

        if (PROJECT_ROOT / ".venv").is_dir():
            oks.append(".venv 虚拟环境已存在")
        else:
            warns.append(".venv 不存在，install 时会自动执行 uv sync")

    # ---- 4. 输出 ----
    result_obj = {
        "配置文件": str(cfg_path),
        "运行模式": mode_cn,
        "影子模式": shadow,
        "结论": "通过" if not problems else "未通过",
        "严重问题": problems,
        "警告": warns,
        "OK 项": oks,
    }
    if args.json:
        print(json.dumps(result_obj, ensure_ascii=False, indent=2))
    else:
        # 人类可读：三栏
        header = "=" * 56
        print(header)
        print(f"  {APP_NAME_CN} · 配置自检报告")
        print(header)
        print(f"  配置文件 : {result_obj['配置文件']}")
        print(f"  运行模式 : {result_obj['运行模式']}")
        if shadow:
            print(f"  影子开关 : {'✅ 已开启（下单/撤单不真发）'}")
        print(f"  结   论  : {'✅ 通过' if not problems else '❌ 未通过'}")
        for title, items, tag in (
            ("严重问题", problems, "FATAL"),
            ("警告    ", warns, "WARN "),
            ("OK 项   ", oks, " OK  "),
        ):
            print(f"  {title}({tag}):")
            if not items:
                print("     —")
            else:
                for it in items:
                    print(f"     · {it}")
        print(header)
        if shadow:
            # 影子专属页脚提示
            tips = [
                "🟡【影子模式】当前不会真发任何订单，放心联调 6-12 小时观察：",
                "   · 日志 grep SHADOW 可核对影子成交序列",
                "   · Dashboard /api/diag → system.runtime_mode = 实盘模式(影子 SHADOW)",
                "   · 观察 OK 后：shadow_mode=false 即可切真做实盘（别忘了再跑一次 ycs check）",
            ]
            for t in tips:
                print("  " + t)
        elif not problems and not live:
            print("  💡 纸盘模式：占位密钥不会阻止启动；如需实盘，请填入真实凭证并切换 live=true。")
        elif problems and live:
            print("  💡 实盘模式：请修正以上 FATAL 项后再次执行 `ycsctl check` 验证。", file=_sys.stderr)

    return 1 if problems else 0


def cmd_status() -> int:
    if not _have("systemctl"):
        print(f"""[info] 当前环境未检测到 systemctl（可能是容器 / 非 systemd 发行版）。
  请改用：
    · 前台：uv run python run.py
    · 手动查看进程：ps -ef | grep -E 'run.py|ycs' | grep -v grep
    · 查看日志：ls -la {PROJECT_ROOT}/logs/""")
        return 0
    r = _systemctl("status", UNIT_NAME, "--no-pager", "--lines=20")
    # 输出直接透传
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        _print_err(r.stderr.rstrip())
    # exit 0/3/4 ... 透传，便于脚本判断
    return r.returncode if 0 <= r.returncode <= 255 else 0


def _cmd_simple(*sub: str, action: str) -> int:
    """start / stop / restart 共用封装。"""
    if not _have("systemctl"):
        _print_err(f"[error] 未检测到 systemctl，无法执行「{action}」。\n"
                   f"  请使用前台方式启动：uv run python {PROJECT_ROOT / 'run.py'}")
        return 2
    r = _systemctl(*sub, UNIT_NAME)
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        # "必须以 root" 时给出友好提示
        stderr = r.stderr.rstrip()
        _print_err(stderr)
        if (r.returncode != 0 and
            ("root" in stderr.lower() or "permission" in stderr.lower() or "privileges" in stderr.lower())):
            _print_err(f"  💡 请以 root 执行：sudo uv run python deploy/ycsctl.py {action}")
    return r.returncode if 0 <= r.returncode <= 255 else 1


def cmd_start() -> int:
    return _cmd_simple("start", action="start")


def cmd_stop() -> int:
    return _cmd_simple("stop", action="stop")


def cmd_restart() -> int:
    return _cmd_simple("restart", action="restart")


def cmd_logs(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if args.follow:
        extra.append("-f")
    if args.n is not None:
        extra += ["-n", str(args.n)]
    if args.since:
        extra += ["--since", args.since]
    r = _journalctl(*extra, "--no-pager")
    return r.returncode if 0 <= r.returncode <= 255 else 1


def cmd_config(args: argparse.Namespace) -> int:
    path = DEFAULT_CONFIG_PATH
    if args.sub == "path":
        print(path)
        return 0
    # 默认 show
    if not path.is_file():
        _print_err(f"[warn] 未找到 {path}")
        return 1
    print(path.read_text(encoding="utf-8").rstrip())
    return 0


def _run_installer(extra: list[str]) -> int:
    if not INSTALL_SCRIPT.is_file():
        _print_err(f"[error] 找不到安装脚本：{INSTALL_SCRIPT}")
        return 2
    if not _have("systemctl"):
        _print_err(f"[error] 未检测到 systemctl（容器环境），无法进行 systemd 安装。\n"
                   f"  前台启动：uv run python {PROJECT_ROOT / 'run.py'}")
        return 2
    try:
        return subprocess.call(["sudo", "bash", str(INSTALL_SCRIPT), *extra])
    except KeyboardInterrupt:
        _print_err("[cancel] 安装已取消")
        return 130


def cmd_install(args: argparse.Namespace) -> int:
    extra = ["--no-enable"] if args.no_enable else []
    return _run_installer(extra)


def cmd_uninstall() -> int:
    return _run_installer(["--uninstall"])


def cmd_kill(args: "argparse.Namespace") -> int:
    """A5 Kill-Switch CLI 通道：
       1) 调 POST /api/kill（127.0.0.1 + Token），让 Controller 内部撤单+全平+写入状态；
       2) 若接口失败（API 已挂/Token 错）→ 写 data/EMERGENCY_HALT 文件兜底；
       3) 再 systemctl stop ycs 防进程再起。
    """
    import json as _json
    import urllib.request as _req
    import urllib.error as _err
    from pathlib import Path as _P

    token = args.token
    cfg_path = DEFAULT_CONFIG_PATH
    if not token and _P(cfg_path).is_file():
        try:
            import yaml as _yaml
            raw = _yaml.safe_load(_P(cfg_path).read_text()) or {}
            token = (raw.get("risk_limits") or {}).get("kill_switch_token")
        except Exception:
            token = None

    host = (args.host or "http://127.0.0.1:8000").rstrip("/")
    _print_err(f"[ycsctl kill] 通道① POST {host}/api/kill ...")
    ok_http = False
    status_code = 0
    body_str = ""
    if token:
        try:
            req = _req.Request(
                f"{host}/api/kill", method="POST",
                headers={"X-YCS-Admin-Token": str(token), "Content-Type": "application/json"},
                data=b"{}",
            )
            with _req.urlopen(req, timeout=15) as resp:
                status_code = int(getattr(resp, "status", 200) or 200)
                body_bytes = resp.read() or b""
                body_str = body_bytes.decode("utf-8", errors="replace") or ""
            ok_http = 200 <= status_code < 300
        except _err.HTTPError as e:
            status_code = int(e.code or 0)
            try:
                body_str = (e.read() or b"").decode("utf-8", errors="replace")
            except Exception:
                body_str = str(e)
        except Exception as e:
            _print_err(f"  HTTP 异常：{type(e).__name__}: {e}（走通道②兜底）")
            status_code = 0
    else:
        _print_err("  未获取到 kill_switch_token（--token 或 config.yaml 都缺失），跳过 HTTP 通道。")

    if ok_http:
        _print_ok(f"✓ /api/kill 返回 HTTP {status_code}：{body_str[:200]}")
    else:
        _print_err(f"✗ /api/kill 失败（HTTP {status_code}）：{body_str[:200]}")
        # 兜底通道②：写 EMERGENCY_HALT 文件
        project_root = _P(__file__).resolve().parent.parent
        halt_path = project_root / "data" / "EMERGENCY_HALT"
        try:
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            import time as _t
            halt_path.write_text(
                f"created_by=ycsctl_kill\nat={int(_t.time())}\nstatus={status_code}\n",
                encoding="utf-8",
            )
            _print_ok(f"✓ 兜底②：写文件 {halt_path} 成功（Controller 每秒轮询到会自停）")
        except Exception as e:
            _print_err(f"✗ 兜底② 写失败：{type(e).__name__}: {e}")

    # 最后兜底③：systemctl stop ycs
    import shutil as _shutil
    if _shutil.which("systemctl"):
        try:
            subprocess.run(["sudo", "-n", "systemctl", "stop", "ycs"], check=False, timeout=30)
            _print_ok("✓ 兜底③：systemctl stop ycs 已执行（无需 sudo 密码则生效）")
        except Exception as e:
            _print_err(f"  systemctl stop 异常：{type(e).__name__}: {e}（非 systemd 环境可忽略）")
    return 0 if ok_http else 2


# ---------------------------------------------------------------------------
# 顶层解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ycsctl",
        description=f"{APP_NAME_CN} · 管理命令行工具 —— 直接 ycs（或 ycsctl menu）进交互式菜单，不用记命令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助")
    sub = parser.add_subparsers(dest="cmd", metavar="<命令>")

    # help
    p_help = sub.add_parser("help", help="显示帮助")
    p_help.set_defaults(func=lambda *a, **k: cmd_help())

    # menu（交互式）
    p_menu = sub.add_parser("menu", help="【推荐】交互式菜单：ycs / ycsctl menu")
    p_menu.set_defaults(func=lambda *a, **k: interactive_cmd_menu())

    # version
    p_ver = sub.add_parser("version", help="显示版本号")
    p_ver.set_defaults(func=lambda *a, **k: cmd_version())

    # check
    p_check = sub.add_parser("check", help="配置自检")
    p_check.add_argument("--json", action="store_true", help="输出 JSON")
    p_check.add_argument("--config", default=None, help=f"指定配置文件路径（默认: {DEFAULT_CONFIG_PATH}）")
    p_check.set_defaults(func=cmd_check)

    # status / start / stop / restart
    p_s = sub.add_parser("status", help="查询服务状态")
    p_s.set_defaults(func=lambda *a, **k: cmd_status())
    p_st = sub.add_parser("start", help="启动 ycs 服务")
    p_st.set_defaults(func=lambda *a, **k: cmd_start())
    p_sp = sub.add_parser("stop", help="停止 ycs 服务")
    p_sp.set_defaults(func=lambda *a, **k: cmd_stop())
    p_r = sub.add_parser("restart", help="重启 ycs 服务（修改配置后执行）")
    p_r.set_defaults(func=lambda *a, **k: cmd_restart())

    # logs
    p_logs = sub.add_parser("logs", help="查看 journalctl 日志")
    p_logs.add_argument("-n", "--lines", dest="n", type=int, default=None,
                        help="显示最近 N 行（默认 journalctl 默认值，推荐 200）")
    p_logs.add_argument("-f", "--follow", action="store_true", help="跟随新日志输出")
    p_logs.add_argument("--since", default=None, help="过滤起始时间，如 '2026-08-28' / '1 hour ago'")
    p_logs.set_defaults(func=cmd_logs)

    # config
    p_cfg = sub.add_parser("config", help="查看 config.yaml")
    p_cfg.add_argument("sub", nargs="?", choices=["show", "path"], default="show",
                       help="show=打印内容, path=只打印路径 (默认 show)")
    p_cfg.set_defaults(func=cmd_config)

    # install / uninstall
    p_ins = sub.add_parser("install", help="一键安装 systemd 服务 (install_systemd.sh)")
    p_ins.add_argument("--no-enable", action="store_true", help="安装但不启用开机自启")
    p_ins.set_defaults(func=cmd_install)
    p_unins = sub.add_parser("uninstall", help="停止并卸载 systemd 服务")
    p_unins.set_defaults(func=lambda *a, **k: cmd_uninstall())

    # kill（紧急停机 · A5 Kill-Switch 三通道之一）
    p_kill = sub.add_parser(
        "kill",
        help="【紧急】一键紧急停机+全平所有仓位（优先调 /api/kill；失败则写 EMERGENCY_HALT + systemctl stop）",
    )
    p_kill.add_argument("--token", default=None,
                        help=f"kill_switch_token（未提供时从 {DEFAULT_CONFIG_PATH}.risk_limits.kill_switch_token 读取）")
    p_kill.add_argument("--host", default="http://127.0.0.1:8000",
                        help="Dashboard API 地址（默认 http://127.0.0.1:8000）")
    p_kill.set_defaults(func=cmd_kill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # 无参数 → 直接进交互式菜单（满足：直接 ycs 回车 弹菜单）
    if not argv:
        return interactive_cmd_menu()
    if argv[0] in ("-h", "--help", "help"):
        return cmd_help()
    if argv[0] in ("-V", "--version"):
        return cmd_version()

    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit as exc:
        # argparse 的错误用中文提醒未知命令
        return int(exc.code) if exc.code is not None else 2

    # 未知子命令：args.cmd == None（已由 argparse 保证非空 argv[0] 合法才走这里）
    if getattr(args, "cmd", None) is None:
        # 说明子命令不存在；argparse 会先报错并 exit=2；这里兜底
        return 2

    func = getattr(args, "func", None)
    if func is None:
        _print_err(f"[error] 未知命令: {args.cmd}")
        _print_err("  可用命令列表请执行：ycsctl help")
        return 2

    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        _print_err("[cancel] 已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
