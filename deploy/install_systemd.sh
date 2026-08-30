#!/usr/bin/env bash
# =============================================================================
# 云龙挑战赛（YCS）一键 systemd 安装脚本
# 用法：
#   cd <项目根目录>（如 /opt/ycs 或 ~/ycs；脚本会自动按 deploy/install_systemd.sh 所在位置推导）
#   bash deploy/install_systemd.sh                # 安装服务 + 启动 + 开机自启
#   bash deploy/install_systemd.sh --no-enable    # 只安装+启动，不做开机自启
#   bash deploy/install_systemd.sh --uninstall    # 停止 + 禁用 + 删除 unit
#
# sudo 场景下找不到 ~/.local/bin/uv？用 env 透传绝对路径（上方 install.sh 默认已做）：
#   sudo env UV_BIN=/root/.local/bin/uv bash deploy/install_systemd.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
# 允许上层 install.sh 通过 env 传 INSTALL_DIR / CONFIG_PATH；未传就按 PROJECT_ROOT 兜底
: "${INSTALL_DIR:=$PROJECT_ROOT}"
: "${CONFIG_PATH:=}"
TEMPLATE="${SCRIPT_DIR}/ycs.service.template"
UNIT_NAME="ycs.service"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}"

# ---- 彩色输出 ----
if [ -t 1 ]; then
  NC='\033[0m' BLD='\033[1m' OK='\033[32m' WARN='\033[33m' ERR='\033[31m'
else
  NC='' BLD='' OK='' WARN='' ERR=''
fi
log()  { echo -e "${BLD}[ycs-install]${NC} $*"; }
ok()   { echo -e "${BLD}[ycs-install]${NC} ${OK}OK${NC}: $*"; }
warn() { echo -e "${BLD}[ycs-install]${NC} ${WARN}WARN${NC}: $*"; }
die()  { echo -e "${BLD}[ycs-install]${NC} ${ERR}FATAL${NC}: $*" >&2; exit 1; }

# ---- 卸载分支 ----
if [[ "${1:-}" == "--uninstall" ]]; then
  log "卸载服务 ${UNIT_NAME} …"
  [ "$(id -u)" -eq 0 ] || die "必须以 root 执行：sudo bash $0 --uninstall"
  systemctl is-active -q "${UNIT_NAME}"    && systemctl stop    "${UNIT_NAME}"    && log "已停止服务" || true
  systemctl is-enabled -q "${UNIT_NAME}"   && systemctl disable "${UNIT_NAME}"    && log "已禁用开机自启" || true
  rm -f "${UNIT_DST}"
  systemctl daemon-reload
  ok "服务 ${UNIT_NAME} 已卸载；${PROJECT_ROOT} 下的数据与配置已保留。"
  exit 0
fi

# ---- 参数 ----
OPT_ENABLE=1
for arg in "$@"; do
  case "$arg" in
    --no-enable) OPT_ENABLE=0 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数：$arg（可用：--no-enable / --uninstall）" ;;
  esac
done

# ---- root 检查 ----
[ "$(id -u)" -eq 0 ] || die "必须以 root 执行：sudo bash $0"

# ---- 解决：sudo 下 secure_path 清掉 PATH → 找不到用户级 ~/.local/bin/uv
# 候选目录搜索（与 install.sh _resolve_uv_bin 一致）
_expand_path_for_uv() {
  local u_home="${HOME:-/root}"
  local sudouser_home=""
  if [ -n "${SUDO_USER:-}" ]; then
    # shellcheck disable=SC2086
    sudouser_home="$(eval echo ~$SUDO_USER 2>/dev/null || true)"
  fi
  local d
  for d in \
    "${u_home}/.local/bin" \
    "${u_home}/.cargo/bin" \
    "${sudouser_home}/.local/bin" \
    "${sudouser_home}/.cargo/bin" \
    "/root/.local/bin" \
    "/root/.cargo/bin" \
    "/usr/local/bin" \
    "/usr/bin"; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    case ":$PATH:" in *":$d:"*) ;; *) export PATH="$d:$PATH" ;; esac
  done
}
_expand_path_for_uv

# ---- 依赖检查：UV_BIN env（来自 install.sh 透传）优先，其次 PATH 内 command -v ----
if [ -n "${UV_BIN:-}" ] && [ -x "$UV_BIN" ]; then
  :   # 上层传过来的绝对路径（最稳），直接信任，跳过 command -v 检查
elif command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
else
  die "未检测到 uv。两种解法二选一：
   1) 先安装：curl -LsSf https://astral.sh/uv/install.sh | sh
   2) 从上层 install.sh 调：sudo env UV_BIN=/root/.local/bin/uv bash deploy/install_systemd.sh
      （把绝对路径透传进来就不会被 sudo secure_path 清掉）"
fi
command -v systemctl   >/dev/null || die "当前环境无 systemctl（容器内不可用 systemd）"
command -v python3     >/dev/null || die "未检测到 python3"

[ -f "${TEMPLATE}" ]   || die "找不到 service 模板：${TEMPLATE}"
[ -f "${PROJECT_ROOT}/run.py" ] || die "找不到 ${PROJECT_ROOT}/run.py"
[ -f "${PROJECT_ROOT}/config.yaml" ] || warn "未检测到 config.yaml；首次启动前请按文档填写 OKX / AI 凭证"

# ---- 自动探测参数 ----
RUN_USER="$(stat -c '%U' "${PROJECT_ROOT}/run.py" 2>/dev/null || echo "$(logname 2>/dev/null || echo root)")"
RUN_PY="${PROJECT_ROOT}/run.py"
# 2026-08-30：不再用 `uv run python`，直接使用 .venv 里的绝对 python 启动。
#   原因：ProtectHome=true 会把 /root/.local 对进程隐藏，ExecStart=/root/.local/bin/uv… → status=203/EXEC。
#   使用 .venv/bin/python 作为单一事实来源，且已在下方 ReadWritePaths 白名单中，namespace 里可见。
VENV_PYTHON="${INSTALL_DIR}/.venv/bin/python"
# 兼容：INSTALL_DIR != PROJECT_ROOT 的罕见场景
if [ ! -x "${VENV_PYTHON}" ] && [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi

log "参数探测："
echo "   User        : ${RUN_USER}"
echo "   WorkingDir  : ${PROJECT_ROOT}"
echo "   uv          : ${UV_BIN}   (仅用于 uv sync，不再用于 systemd ExecStart)"
echo "   .venv/python: ${VENV_PYTHON}"
echo "   run.py      : ${RUN_PY}"
echo "   Enable-once : ${OPT_ENABLE}"

# ---- 依赖（虚拟环境）----
if [ ! -x "${VENV_PYTHON}" ]; then
  log "首次安装：以 ${RUN_USER} 身份执行 uv sync 创建 .venv …"
  # 用 $UV_BIN 绝对路径（避开 su 下 PATH 丢失问题）
  su -s /bin/bash "${RUN_USER}" -c "cd '${PROJECT_ROOT}' && '${UV_BIN}' sync"
fi
# 再次定位（uv sync 成功后 INSTALL_DIR/.venv 可能才被创建）
if [ -x "${INSTALL_DIR}/.venv/bin/python" ]; then
  VENV_PYTHON="${INSTALL_DIR}/.venv/bin/python"
elif [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
fi
[ -x "${VENV_PYTHON}" ] || die ".venv/bin/python 不存在（${VENV_PYTHON}）：uv sync 可能失败，请手动 cd ${PROJECT_ROOT} && ${UV_BIN} sync 查看详细错误。"
# 额外 sanity：.venv 能成功 import uvicorn / fastapi（缺依赖就提早失败，别等 systemd 203/EXEC 了才发现）
if ! "${VENV_PYTHON}" -c "import uvicorn, fastapi, yaml, pydantic" >/dev/null 2>&1; then
  die ".venv 依赖校验失败（uvicorn/fastapi/yaml/pydantic 任一不可 import）。请先执行：cd ${PROJECT_ROOT} && ${UV_BIN} sync"
fi
# 冒烟：.venv/bin/python /path/to/run.py --help 能成功解析参数（证明 WorkingDirectory 下 run.py 语法/依赖就绪）
if ! "${VENV_PYTHON}" "${RUN_PY}" --help >/dev/null 2>&1; then
  die "run.py 冒烟失败：${VENV_PYTHON} ${RUN_PY} --help 返回非 0。请手动跑一次看异常。"
fi

# ======= 必须在渲染 systemd unit 之前，提前创建 ReadWritePaths / WorkingDirectory 内所需目录：
#         ProtectSystem=strict + ReadWritePaths 时，namespace 阶段要求路径必须存在，
#         否则直接 status=226/NAMESPACE，进程根本起不来（= 用户看到的 2845 次重启 + curl HTTP 000）
log "确保写入目录存在：data / logs / .venv …"
mkdir -p "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/.venv"
# 兜底：如果 RUN_USER 不是当前文件 owner（例如 git clone 后全是 root，但 RUN_USER=yunlong），统一 chown 一遍
#   拿到 RUN_USER 对应的主 group：用 id -gn（succinct 且能处理 user 不是 owner 的情况）
RUN_GROUP="$(id -gn "${RUN_USER}" 2>/dev/null || echo "${RUN_USER}")"
chown -R "${RUN_USER}:${RUN_GROUP}" "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/.venv" 2>/dev/null || true
# 额外：保证项目根本身 RUN_USER 可进入（读文件/exec venv 脚本/写日志时必须）
chown "${RUN_USER}:${RUN_GROUP}" "${PROJECT_ROOT}" 2>/dev/null || true
ok "写入目录就绪（data/logs/.venv 存在，owner=${RUN_USER}:${RUN_GROUP}）"

# ---- unit 渲染（通过 Python 做模板注入，避免 sed 边界 bug）----
log "生成 ${UNIT_DST}（使用 Python 模板注入）…"
RENDERED="$(python3 - "$TEMPLATE" "$RUN_USER" "$PROJECT_ROOT" "$VENV_PYTHON" "$RUN_PY" <<'PY'
# 2026-08-30：所有 import 写顶部，避免 VPS 系统 python3 跑进来漏 shutil / os（之前 NameError: shutil is not defined）
import sys, os, pathlib, shlex, shutil, subprocess  # noqa: F401  (subprocess 占位：将来延伸时用；目前防漏导入)
tpl = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
user, workdir, venv_python, run_py = sys.argv[2:6]
# Python dict 作为单一事实来源注入
replacements = {
    "__USER__": user,
    "__WORKDIR__": workdir,
    "__VENV_PYTHON__": venv_python,
    "__RUN_PY__": run_py,
}
missing = [k for k in replacements if k not in tpl]
if missing:
    print("ERROR: 模板缺少占位符: " + ", ".join(missing), file=sys.stderr)
    sys.exit(3)
for k, v in replacements.items():
    tpl = tpl.replace(k, v)
# 语法级 sanity：[Unit]/[Service]/[Install] 三段都得出现
for section in ("[Unit]", "[Service]", "[Install]"):
    if section not in tpl:
        print(f"ERROR: 渲染结果缺少 {section}", file=sys.stderr)
        sys.exit(4)
# 关键 sanity：渲染后的非注释行不能包含未替换占位符（注释行以 # 开头，放过，避免历史说明里出现 __UV__ 这类字样误判）
for leftover in ("__UV__", "__USER__", "__WORKDIR__", "__VENV_PYTHON__", "__RUN_PY__"):
    for ln, raw in enumerate(tpl.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if leftover in s:
            print(f"ERROR: 渲染仍残留占位符 {leftover} (第 {ln} 行: {raw})", file=sys.stderr)
            sys.exit(5)
# ExecStart 与 ExecCondition 必须真实存在/可执行（等价于本地提前模拟 systemd exec()）
for line in tpl.splitlines():
    s = line.strip()
    if s.startswith("ExecStart="):
        parts = shlex.split(s[len("ExecStart="):])
        if not parts:
            print("ERROR: ExecStart 为空", file=sys.stderr); sys.exit(6)
        exe = parts[0]
        if not pathlib.Path(exe).is_file():
            print(f"ERROR: ExecStart 二进制不存在: {exe}", file=sys.stderr); sys.exit(6)
        if not os.access(exe, os.X_OK):
            print(f"ERROR: ExecStart 二进制不可执行(+x): {exe}", file=sys.stderr); sys.exit(6)
    elif s.startswith("ExecCondition="):
        # ExecCondition=test -x <path>：保证 test 可执行 + 目标条件满足
        parts = shlex.split(s[len("ExecCondition="):])
        if not parts:
            print("ERROR: ExecCondition 为空", file=sys.stderr); sys.exit(7)
        test_bin = shutil.which(parts[0]) or parts[0]
        if not pathlib.Path(test_bin).is_file():
            print(f"ERROR: ExecCondition 二进制不存在: {test_bin}", file=sys.stderr); sys.exit(7)
        # test -x/-f/-d：验证目标也真实存在（= systemd exec 前执行的同一套语义）
        if parts[0] == "test" and len(parts) == 3 and parts[1] in ("-x", "-f", "-d"):
            target = parts[2]
            if parts[1] == "-x" and not (pathlib.Path(target).is_file() and os.access(target, os.X_OK)):
                print(f"ERROR: ExecCondition test -x 失败: {target}", file=sys.stderr); sys.exit(7)
            if parts[1] == "-f" and not pathlib.Path(target).is_file():
                print(f"ERROR: ExecCondition test -f 失败: {target}", file=sys.stderr); sys.exit(7)
            if parts[1] == "-d" and not pathlib.Path(target).is_dir():
                print(f"ERROR: ExecCondition test -d 失败: {target}", file=sys.stderr); sys.exit(7)
sys.stdout.write(tpl)
PY
)" || die "渲染模板失败：请修上方 Python 错误。若报 ExecCondition/ExecStart 的 xxx 不存在 → 先手动 mkdir -p ${PROJECT_ROOT}/data ${PROJECT_ROOT}/logs 并确认 ${VENV_PYTHON} 存在。"

# 写入
printf '%s' "${RENDERED}" > "${UNIT_DST}"
chmod 644 "${UNIT_DST}"
log "写入 ${UNIT_DST} 完成"

# ---- 启动 ----
systemctl daemon-reload
systemctl enable  "${UNIT_NAME}" 2>/dev/null || true
systemctl restart "${UNIT_NAME}"
ok "服务已重启"

# 等待 6 秒后报告状态
sleep 6
echo
echo "================= systemctl status ================="
systemctl status --no-pager "${UNIT_NAME}" || true
echo
echo "================= 最近 30 行日志 =================="
journalctl -u "${UNIT_NAME}" -n 30 --no-pager || true
echo

# ---- 开机自启 ----
if [ "${OPT_ENABLE}" -eq 1 ]; then
  systemctl is-enabled -q "${UNIT_NAME}" || systemctl enable "${UNIT_NAME}" >/dev/null
  ok "已启用开机自启（multi-user.target）"
else
  log "--no-enable：未启用开机自启，后续执行 systemctl enable ycs 开启。"
fi

echo
ok "部署完成。常用命令："
echo "  systemctl status  ycs        # 查看状态"
echo "  systemctl restart ycs        # 重启（修改 config.yaml 后执行）"
echo "  journalctl -u ycs -fn        # 实时看日志"
echo "  sudo bash deploy/install_systemd.sh --uninstall  # 卸载"
