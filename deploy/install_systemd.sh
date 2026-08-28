#!/usr/bin/env bash
# =============================================================================
# 云龙挑战赛（YCS）一键 systemd 安装脚本
# 用法：
#   cd /workspace
#   bash deploy/install_systemd.sh                # 安装服务 + 启动 + 开机自启
#   bash deploy/install_systemd.sh --no-enable    # 只安装+启动，不做开机自启
#   bash deploy/install_systemd.sh --uninstall    # 停止 + 禁用 + 删除 unit
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
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

# ---- uninstall ----
if [[ "${1:-}" == "--uninstall" ]]; then
  log "卸载服务 ${UNIT_NAME} …"
  [ "$(id -u)" -eq 0 ] || die "必须以 root 执行：sudo bash $0 --uninstall"
  systemctl is-active -q "${UNIT_NAME}"    && systemctl stop    "${UNIT_NAME}"    && log "已停止服务" || true
  systemctl is-enabled -q "${UNIT_NAME}"   && systemctl disable "${UNIT_NAME}"    && log "已禁用开机自启" || true
  rm -f "${UNIT_DST}"
  systemctl daemon-reload
  ok "服务 ${UNIT_NAME} 已卸载；/workspace 下的数据与配置已保留。"
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

# ---- 依赖检查 ----
command -v uv          >/dev/null || die "未检测到 uv，先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v systemctl   >/dev/null || die "当前环境无 systemctl（容器内不可用 systemd）"
command -v python3     >/dev/null || die "未检测到 python3"

[ -f "${TEMPLATE}" ]   || die "找不到 service 模板：${TEMPLATE}"
[ -f "${PROJECT_ROOT}/run.py" ] || die "找不到 ${PROJECT_ROOT}/run.py"
[ -f "${PROJECT_ROOT}/config.yaml" ] || warn "未检测到 config.yaml；首次启动前请按文档填写 OKX / AI 凭证"

# ---- 自动探测参数 ----
RUN_USER="$(stat -c '%U' "${PROJECT_ROOT}/run.py" 2>/dev/null || echo "$(logname 2>/dev/null || echo root)")"
UV_BIN="$(command -v uv || echo /usr/local/bin/uv)"
RUN_PY="${PROJECT_ROOT}/run.py"

log "参数探测："
echo "   User        : ${RUN_USER}"
echo "   WorkingDir  : ${PROJECT_ROOT}"
echo "   uv          : ${UV_BIN}"
echo "   run.py      : ${RUN_PY}"
echo "   Enable-once : ${OPT_ENABLE}"

# ---- 依赖（虚拟环境）----
if [ ! -d "${PROJECT_ROOT}/.venv" ]; then
  log "首次安装：以 ${RUN_USER} 身份执行 uv sync 创建 .venv …"
  su -s /bin/bash "${RUN_USER}" -c "cd '${PROJECT_ROOT}' && uv sync"
fi
[ -x "${PROJECT_ROOT}/.venv/bin/python" ] || die ".venv/bin/python 不存在，uv sync 可能失败"

# ---- unit 渲染（通过 Python 做参数注入，避免 sed 边界 bug）----
log "生成 ${UNIT_DST}（使用 Python 模板注入）…"
RENDERED="$(python3 - "$TEMPLATE" "$RUN_USER" "$PROJECT_ROOT" "$UV_BIN" "$RUN_PY" <<'PY'
import sys, pathlib
tpl = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
user, workdir, uv, run_py = sys.argv[2:6]
# Python dict 作为单一事实来源注入（符合经验 1514370：避免手写 sed 逐行漂移）
replacements = {
    "__USER__": user,
    "__WORKDIR__": workdir,
    "__UV__": uv,
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
sys.stdout.write(tpl)
PY
)" || die "渲染模板失败，请检查上方 Python 异常。"

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
