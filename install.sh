#!/usr/bin/env bash
# =============================================================================
# 云龙挑战赛（YCS）VPS 一键安装/更新脚本 · install.sh
#
#  · 首次：不存在 INSTALL_DIR/.git → git clone → uv sync → pytest → systemd → 启动
#  · 更新：已存在仓库 → dirty 检查 → git fetch + pull --ff-only → uv sync → pytest → restart
#  · 幂等：任何中断后重跑都安全
#  · 代理：HTTP_PROXY/HTTPS_PROXY/all_proxy 已配置时，自动注入 git http(s).proxy
#
# 推荐用法（把 YOUR_GITHUB_USERNAME / YOUR_GITHUB_REPO 改成你真实值）：
#
#   curl -fsSL \
#     https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_GITHUB_REPO/main/install.sh \
#     | GIT_REPO=https://github.com/YOUR_GITHUB_USERNAME/YOUR_GITHUB_REPO.git bash
#
#   或两步（便于后续重跑）：
#     curl -fsSL <raw地址> -o install.sh
#     GIT_REPO=https://github.com/<user>/<repo>.git bash install.sh
#
# 常用环境变量：
#   GIT_REPO              仓库 HTTPS/SSH 地址（默认是占位，不替换会立刻终止）
#   GIT_BRANCH            分支名，默认 main
#   INSTALL_DIR           安装目录，root 场景默认 /opt/ycs，其它默认 $HOME/ycs
#   YCS_SKIP_TEST=1       跳过 pytest（首次/更新前推荐先不跳过）
#   YCS_NO_SYSTEMD=1      不部署 systemd（容器、桌面环境）
#   YCS_FORCE=1           强制更新：dirty 仓库自动 stash，pull --ff-only 失败则 reset --hard
#   HTTP_PROXY / HTTPS_PROXY / all_proxy   下载 GitHub 慢时，设置代理（脚本会透传给 git）
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 0.5) UV_BIN / SUDO 辅助：解决「用户级安装 uv 到 ~/.local/bin」后，
#      sudo bash / sudo <命令> 时 sudo secure_path 把 PATH 清掉导致
#      `sudo: uv: command not found` / `[ycs-install] FATAL: 未检测到 uv` 连锁失败。
#      VPS 场景（用户直接 root 跑 / 非 root 但 sudo 到 root 装 systemd）全覆盖。
# ---------------------------------------------------------------------------
# 候选 uv 安装目录：官方 install.sh 默认写 ~/.local/bin；cargo 源会在 ~/.cargo/bin；
# 包管理器 / 手动复制常见落点：/usr/local/bin / usr/bin。按顺序先搜到即命中。
_resolve_uv_bin() {
  # 先在当前 PATH 里找（最快）
  local p
  p="$(command -v uv 2>/dev/null || true)"
  if [ -n "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
  local u_home="${HOME:-/root}"
  local sudouser_home=""
  if [ -n "${SUDO_USER:-}" ]; then
    # shellcheck disable=SC2086
    sudouser_home="$(eval echo ~$SUDO_USER 2>/dev/null || true)"
  fi
  for d in \
    "${u_home}/.local/bin" \
    "${u_home}/.cargo/bin" \
    "${sudouser_home}/.local/bin" \
    "${sudouser_home}/.cargo/bin" \
    "/root/.local/bin" \
    "/root/.cargo/bin" \
    "/usr/local/bin" \
    "/usr/bin"; do
    [ -z "$d" ] && continue
    if [ -x "${d}/uv" ]; then echo "${d}/uv"; return 0; fi
  done
  return 1
}
# SUDO：已经是 root 就空字符串（避免 sudo 再跑一次清 PATH 的问题）；非 root 才 sudo
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi
# PATH 膨胀：把上面候选 uv 目录全部追加进 PATH（本脚本后续裸 `uv` 调用也能 work）
for _d in \
  "${HOME:-/root}/.local/bin" \
  "${HOME:-/root}/.cargo/bin" \
  "/root/.local/bin" \
  "/root/.cargo/bin" \
  "/usr/local/bin"; do
  [ -n "$_d" ] && [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) export PATH="$_d:$PATH" ;; esac
done
unset _d

# ---------------------------------------------------------------------------
# 0) 参数默认值（环境变量优先）
# ---------------------------------------------------------------------------
: "${GIT_REPO:=https://github.com/YOUR_GITHUB_USERNAME/YOUR_GITHUB_REPO.git}"
: "${GIT_BRANCH:=main}"
if [ "$(id -u)" -eq 0 ]; then
  : "${INSTALL_DIR:=/opt/ycs}"
else
  : "${INSTALL_DIR:=$HOME/ycs}"
fi
: "${YCS_SKIP_TEST:=0}"
: "${YCS_NO_SYSTEMD:=0}"
: "${YCS_FORCE:=0}"

# ---------------------------------------------------------------------------
# 1) 彩色日志（非 TTY 关闭颜色，便于 journalctl 阅读）
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_BLD="$(printf '\033[1m')"      ; C_RST="$(printf '\033[0m')"
  C_RED="$(printf '\033[31m')"    ; C_GRN="$(printf '\033[32m')"
  C_YLW="$(printf '\033[33m')"    ; C_CYN="$(printf '\033[36m')"
else
  C_BLD=""; C_RST=""; C_RED=""; C_GRN=""; C_YLW=""; C_CYN=""
fi
PREFIX="${C_BLD}[install.sh]${C_RST}"
log_i() { printf '%b %bINFO%b: %s\n' "${PREFIX}" "${C_CYN}" "${C_RST}" "$*"; }
log_o() { printf '%b   %bOK%b: %s\n' "${PREFIX}" "${C_GRN}" "${C_RST}" "$*"; }
log_w() { printf '%b %bWARN%b: %s\n' "${PREFIX}" "${C_YLW}" "${C_RST}" "$*" 1>&2; }
hr()    { printf '────────────────────────────────────────────────────────────\n'; }
die()   {
  printf '%b  %bFATAL%b: %s\n' "${PREFIX}" "${C_RED}" "${C_RST}" "$1" 1>&2
  shift
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@" 1>&2
  fi
  exit 1
}

# ---------------------------------------------------------------------------
# 2) 基础依赖检查（curl/git/python3）
# ---------------------------------------------------------------------------
need() { command -v "$1" >/dev/null; }
need git     || die "未检测到 git"     "请执行：sudo apt-get update && sudo apt-get install -y git curl ca-certificates python3"
need curl    || die "未检测到 curl"    "请执行：sudo apt-get install -y curl ca-certificates"
need python3 || die "未检测到 python3" "请执行：sudo apt-get install -y python3 python3-pip python3-venv"

# ---------------------------------------------------------------------------
# 3) GIT_REPO 占位拦截
# ---------------------------------------------------------------------------
case "$GIT_REPO" in
  *"YOUR_GITHUB_USERNAME"* | *"YOUR_GITHUB_REPO"*)
    hr
    die "GIT_REPO 仍是占位值（YOUR_GITHUB_USERNAME / YOUR_GITHUB_REPO），请先替换。" \
        "" \
        "推荐步骤：" \
        "  1) 把本项目 push 到你自己的 GitHub 仓库；" \
        "  2) VPS 上执行：" \
        "       GIT_REPO=https://github.com/<用户名>/<仓库名>.git bash install.sh" \
        "  3) 或修改 install.sh 顶部的 GIT_REPO 默认值后 curl | bash。" \
        "" \
        "参考（一键管道式，需替换 3 处 <...>）：" \
        "  curl -fsSL https://raw.githubusercontent.com/<用户名>/<仓库名>/main/install.sh \\" \
        "    | GIT_REPO=https://github.com/<用户名>/<仓库名>.git bash"
    ;;
esac

# ---------------------------------------------------------------------------
# 4) 代理透传（经验 1053955）：HTTP_PROXY 等存在时给 git 全局设置 http(s).proxy
# ---------------------------------------------------------------------------
setup_git_proxy() {
  local proxy=""
  [ -n "${HTTPS_PROXY:-}" ]  && proxy="${HTTPS_PROXY}"
  [ -n "${https_proxy:-}" ]  && proxy="${https_proxy}"
  [ -n "${HTTP_PROXY:-}" ]   && proxy="${HTTP_PROXY}"
  [ -n "${http_proxy:-}" ]   && proxy="${http_proxy}"
  [ -n "${all_proxy:-}" ]    && proxy="${all_proxy}"
  [ -n "${ALL_PROXY:-}" ]    && proxy="${ALL_PROXY}"
  if [ -n "$proxy" ]; then
    log_i "检测到代理变量，设置 git http.proxy=$proxy（仅本次会话生效，操作后还原）"
    git config --global --replace-all http.proxy  "$proxy"
    git config --global --replace-all https.proxy "$proxy"
  fi
}
unset_git_proxy() {
  git config --global --unset-all http.proxy  2>/dev/null || true
  git config --global --unset-all https.proxy 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 5) 首次 / 更新 分流
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$INSTALL_DIR")" 2>/dev/null || true

IS_FIRST=0
if [ ! -d "$INSTALL_DIR/.git" ]; then
  # 分支 5B：首次安装
  IS_FIRST=1
  hr
  log_i "首次安装 → 克隆仓库 $GIT_REPO（branch=$GIT_BRANCH, --depth=1）到 $INSTALL_DIR"
  setup_git_proxy
  git clone --depth=1 --branch "$GIT_BRANCH" --single-branch "$GIT_REPO" "$INSTALL_DIR" \
    || die "git clone 失败（检查仓库地址/分支/权限/网络代理）"
  log_o "克隆完成"
  unset_git_proxy
  cd "$INSTALL_DIR"
else
  # 分支 5A：更新流程
  hr
  log_i "检测到已有仓库 $INSTALL_DIR → 执行增量更新"
  cd "$INSTALL_DIR"

  # dirty 检查
  if ! git diff --quiet || ! git diff --cached --quiet; then
    if [ "$YCS_FORCE" -eq 1 ]; then
      log_w "本地有未提交修改，YCS_FORCE=1 → git stash 保留（install.sh-snapshot-时间戳）"
      git stash push -m "install.sh-snapshot-$(date +%Y%m%d-%H%M%S)" || log_w "stash 失败，继续"
    else
      die "检测到本地未提交修改（dirty），为避免覆盖已中断。" \
          "" \
          "推荐：cd $INSTALL_DIR && git stash 后重跑本脚本" \
          "强制：YCS_FORCE=1 bash install.sh（自动 stash）"
    fi
  fi

  # origin 切换到目标 GIT_REPO
  local_url="$(git remote get-url origin 2>/dev/null || true)"
  if [ -n "$local_url" ] && [ "$local_url" != "$GIT_REPO" ]; then
    log_i "origin 当前=$local_url，切换到目标=$GIT_REPO"
    git remote set-url origin "$GIT_REPO"
  fi

  setup_git_proxy
  log_i "git fetch origin $GIT_BRANCH → pull --ff-only"
  git fetch origin "$GIT_BRANCH" || die "git fetch 失败（检查分支名/网络/代理/权限）"
  if git show-ref --verify --quiet "refs/heads/$GIT_BRANCH"; then
    git checkout "$GIT_BRANCH"
  else
    git checkout -b "$GIT_BRANCH" "origin/$GIT_BRANCH" || die "无法切换/创建本地分支 $GIT_BRANCH"
  fi
  if ! git merge-base --is-ancestor HEAD "origin/$GIT_BRANCH"; then
    if [ "$YCS_FORCE" -eq 1 ]; then
      log_w "本地领先远端；YCS_FORCE=1 → 重置为 origin/$GIT_BRANCH（本地未 push 提交将丢失）"
      git reset --hard "origin/$GIT_BRANCH"
    else
      die "本地 HEAD 领先于远端 origin/$GIT_BRANCH。" \
          "请先 cd $INSTALL_DIR && git push；或使用 YCS_FORCE=1 强制重置。"
    fi
  fi
  if ! git pull --ff-only origin "$GIT_BRANCH"; then
    die "pull --ff-only 失败：本地与远端存在分叉。" \
        "请 cd $INSTALL_DIR 手动处理后重跑；或 YCS_FORCE=1 bash install.sh 强制 reset。"
  fi
  log_o "更新完成，当前 HEAD=$(git rev-parse --short HEAD)"
  unset_git_proxy
fi

# ---------------------------------------------------------------------------
# 6) 项目合法性校验（防止 INSTALL_DIR 指错目录）
# ---------------------------------------------------------------------------
for f in run.py deploy/ycsctl.py deploy/install_systemd.sh; do
  [ -f "$f" ] || die "$INSTALL_DIR 不是合法 YCS 项目根目录（缺少 $f）。请检查 INSTALL_DIR / 仓库内容。"
done
[ -f config.yaml.example ] || die "缺少 config.yaml.example（仓库模板缺失，项目非法）。"
if [ ! -f config.yaml ]; then
  log_i "未找到 config.yaml → 自动 cp config.yaml.example → config.yaml（首次安装，全占位纸盘模式）"
  cp config.yaml.example config.yaml || die "cp config.yaml.example config.yaml 失败，请手动执行：cp config.yaml.example config.yaml"
fi
log_o "项目目录校验通过（config.yaml / config.yaml.example 就绪）"

# ---------------------------------------------------------------------------
# 7) 依赖：uv + uv sync
# ---------------------------------------------------------------------------
hr
# 用 _resolve_uv_bin 比 need uv 更稳：即使 PATH 里没有，也能搜到用户级安装的 uv
UV_BIN=""
if UV_BIN="$(_resolve_uv_bin 2>/dev/null || true)"; [ -n "$UV_BIN" ] && [ -x "$UV_BIN" ]; then
  :
else
  log_i "未检测到 uv → 执行官方安装脚本"
  setup_git_proxy
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    || die "uv 安装失败。请手动运行：curl -LsSf https://astral.sh/uv/install.sh | sh"
  unset_git_proxy
  # 官方安装脚本写 ~/.local/bin，再搜一次就拿到了
  UV_BIN="$(_resolve_uv_bin 2>/dev/null || true)"
  if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
    die "uv 已执行 install.sh，但仍找不到 uv 可执行文件。" \
        "请：1) 重开终端或 source ~/.bashrc；2) 确认 ~/.local/bin/uv 存在且可执行。"
  fi
fi
# 把找到的 UV_BIN 再次 export（保证本脚本后续裸 uv 调用和 systemd 子脚本都能拿到绝对路径）
export UV_BIN
log_o "uv 就绪：$("$UV_BIN" --version)  ($UV_BIN)"

log_i "uv sync（创建 .venv / 对齐 lock 版本）"
"$UV_BIN" sync || die "uv sync 失败，常见原因：网络（配置 HTTP_PROXY=host:port）/Python 版本不匹配"
log_o ".venv 就绪"

# ---------------------------------------------------------------------------
# 7.2) 全局命令：ycs / ycsctl（pyproject [project.scripts] 入口 + ~/.local/bin shim）
#
# 用户指令："命令或许记不住 写个菜单 通过选项选择执行，ycs 呼出菜单"。
#   · uv run 或 .venv/bin/activate 后：ycs / ycsctl 直接来自 [project.scripts]
#     （venv/bin/ycs、venv/bin/ycsctl）
#   · 新 shell 登录未 source .venv：写 $HOME/.local/bin/ycs 和 ycsctl 小 shim，
#     自动切到 $INSTALL_DIR 用 `uv run ycs` 跑。新 ssh 登录直接敲 ycs 即可。
# ---------------------------------------------------------------------------
_YCS_LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$_YCS_LOCAL_BIN"
case ":$PATH:" in
  *":$_YCS_LOCAL_BIN:"*) ;;
  *)
    if ! grep -q '$HOME/.local/bin' ~/.bashrc 2>/dev/null; then
      log_i "把 ~/.local/bin 追加进 PATH（写进 ~/.bashrc 永久生效）"
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    fi
    export PATH="$_YCS_LOCAL_BIN:$PATH"
    ;;
esac

# 生成两个 shim：ycs / ycsctl
_shim_content="#!/usr/bin/env bash
# 自动生成的 ycs/ycsctl shim（install.sh 部署时写入 $_YCS_LOCAL_BIN）。
# 作用：用户新 SSH 登录没 source .venv，也能直接敲 ycs → 走项目 uv run。
set -e
INSTALL_DIR=\"$INSTALL_DIR\"
cd \"\$INSTALL_DIR\"
if [ -n \"\${VIRTUAL_ENV:-}\" ] && command -v ycs >/dev/null 2>&1; then
  exec ycs \"\$@\"
fi
exec uv run ycs \"\$@\"
"

for _shim_name in ycs ycsctl; do
  _shim_path="$_YCS_LOCAL_BIN/$_shim_name"
  # pyproject.scripts 里两个名字完全等价，所以这里 shim 也完全一样；
  # 以后若需改名只需循环里加名字即可。
  printf '%s\n' "$_shim_content" > "$_shim_path"
  chmod +x "$_shim_path"
done
log_o "全局命令就绪：ycs（交互菜单首选）/ ycsctl（脚本友好）。试试："
log_o "    ycs           # 直接弹交互菜单（不用记命令）"
log_o "    ycs check     # 配置自检（脚本/CI 友好）"
log_o "    ycsctl kill   # 紧急停机+全平"

# ---------------------------------------------------------------------------
# 7.5) 真实 OKX 历史 K 线 Fixtures —— 用户 2026-08-29 明确：跳过
#   「历史数据+pytest 有点多余，抓紧上实盘」，因此 install.sh 不再
#   要求部署 tests/fixtures/market_data/ 下的 18 个 CSV.GZ，也不再跑
#   deploy/pull_real_okx_klines.py。若后续确实需要离线回测再按需引入。
# ---------------------------------------------------------------------------
hr
log_w "Fixtures：已按用户要求 SKIP（不部署 18×真实 OKX K 线，不跑 stage8/stage9）"

# ---------------------------------------------------------------------------
# 8) pytest（冒烟集：只跑风控/dashboard/ycsctl/config/config_ignore/core
#    基础闭环等必须过的子集；其它 stage 不再阻塞实盘部署）
# ---------------------------------------------------------------------------
hr
PYTEST_DEFAULT_TARGETS=(
  "tests/test_stage1_core.py"
  "tests/test_stage1_broker.py"
  "tests/test_stage2_order_manager.py"
  "tests/test_stage2_risk_pm.py"
  "tests/test_stage4_closed_loop.py"
  "tests/test_stage5_safety_and_dashboard.py"
  "tests/test_stage6_ycsctl.py"
  "tests/test_stage10_risk_controls_and_diag.py"
  "tests/test_stage11_config_yaml_ignore.py"
  "tests/test_stage12_diag_bugfixes.py"
  "tests/test_stage1to3_integrated.py"
  "tests/test_stage7_install_sh.py"
)
if [ "$YCS_SKIP_TEST" -eq 1 ]; then
  log_w "YCS_SKIP_TEST=1 → 跳过 pytest（不推荐）"
else
  log_i "运行 pytest 风控+诊断冒烟子集…"
  # 显式 cd 到 $INSTALL_DIR + 把它放到 PYTHONPATH 首位：
  #   这样测试里 Path(__file__).resolve().parent.parent == REPO 始终成立
  #   （兼容 VPS /opt/ycs、容器 /app、本地任意目录部署，不再依赖硬编码 /workspace）
  cd "$INSTALL_DIR"
  export PYTHONPATH="$INSTALL_DIR${PYTHONPATH:+:$PYTHONPATH}"
  if "$UV_BIN" run pytest "${PYTEST_DEFAULT_TARGETS[@]}" -q --no-header; then
    log_o "pytest 风控/诊断子集全部通过"
  else
    die "pytest 子集失败，已中止部署（避免 bug 版本进实盘）。" \
        "· 确认为环境问题（如 systemd 缺失 / 代理不可达）：YCS_SKIP_TEST=1 bash install.sh" \
        "· 确认为代码问题：先修复 push 到仓库，再到 VPS 重跑 install.sh。"
  fi
fi

# ---------------------------------------------------------------------------
# 9) config.yaml 占位提示
# ---------------------------------------------------------------------------
hr
if [ -f config.yaml ] && grep -q "YOUR_OKX_API_KEY" config.yaml; then
  log_w "config.yaml 仍是占位密钥。纸盘模式可直接跑，实盘需先填 OKX / AI 凭证。"
  echo "    编辑文件：vim $INSTALL_DIR/config.yaml"
  echo "    自检确认：uv run deploy/ycsctl.py check"
fi

# ---------------------------------------------------------------------------
# 10) systemd / 前台
# ---------------------------------------------------------------------------
hr
if [ "$YCS_NO_SYSTEMD" -eq 1 ]; then
  log_w "YCS_NO_SYSTEMD=1 → 跳过 systemd 部署。"
  echo "前台启动：cd $INSTALL_DIR && uv run python run.py"
  exit 0
fi
if ! need systemctl; then
  log_w "未检测到 systemctl（容器/非 systemd 发行版）→ 跳过 systemd 部署。"
  echo "前台启动：cd $INSTALL_DIR && uv run python run.py"
  exit 0
fi

if [ "$IS_FIRST" -eq 1 ]; then
  log_i "首次安装 → 调用 deploy/install_systemd.sh（若提示密码，请输入 sudo 密码）"
  # 透传 UV_BIN + INSTALL_DIR：sudo 会清空用户级 PATH，子脚本在 secure_path 下找不到 ~/.local/bin/uv
  # shellcheck disable=SC2086
  $SUDO env UV_BIN="$UV_BIN" INSTALL_DIR="$INSTALL_DIR" CONFIG_PATH="${CONFIG_PATH:-}" \
    bash deploy/install_systemd.sh || die "install_systemd.sh 失败，见上方输出"
  log_o "部署完成；系统状态摘要："
  systemctl status ycs --no-pager --lines=10 || true
else
  log_i "更新完成 → ycsctl restart（加载新代码）"
  # 主路径：ycsctl restart（UV_BIN 绝对路径 → sudo 下也不会被清 PATH）
  # shellcheck disable=SC2086
  if $SUDO env UV_BIN="$UV_BIN" "$UV_BIN" run python deploy/ycsctl.py restart; then
    log_o "ycs 服务重启成功"
  else
    log_w "ycsctl restart 失败（可能 systemd unit 尚未安装），改为首次执行 install_systemd.sh"
    # shellcheck disable=SC2086
    $SUDO env UV_BIN="$UV_BIN" INSTALL_DIR="$INSTALL_DIR" CONFIG_PATH="${CONFIG_PATH:-}" \
      bash deploy/install_systemd.sh || die "install_systemd.sh 兜底失败，见上方输出"
  fi
fi

# ---------------------------------------------------------------------------
# 11) 收尾提示
# ---------------------------------------------------------------------------
hr
log_o "安装/更新流程结束 ✅"
cat <<'INSTALLSH_END'

  常用命令：
    cd <INSTALL_DIR>
    uv run deploy/ycsctl.py check         配置自检报告（中文）
    uv run deploy/ycsctl.py status        查询 systemd 服务状态
    uv run deploy/ycsctl.py logs -n 200   最近 200 行日志
    uv run deploy/ycsctl.py logs -f       实时跟随日志
    curl http://127.0.0.1:8765/           Dashboard 首页（中文界面）
    curl http://127.0.0.1:8765/api/status JSON 格式总览

  实盘前必须：
    1) uv run deploy/ycsctl.py check   —— 确保无任何 FATAL
    2) 先用纸盘模式（live=false）观察至少 1 天，确认逻辑 / 收益 / 风控符合预期
    3) 再切 live=true：vim config.yaml → trading.live: true
    4) 重启服务：uv run deploy/ycsctl.py restart
INSTALLSH_END
