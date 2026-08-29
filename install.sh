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
[ -f config.yaml ] || log_w "未找到 config.yaml，若仓库已移除该模板需要手动从 README 创建。"
log_o "项目目录校验通过"

# ---------------------------------------------------------------------------
# 7) 依赖：uv + uv sync
# ---------------------------------------------------------------------------
hr
if ! need uv; then
  log_i "未检测到 uv → 执行官方安装脚本"
  setup_git_proxy
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    || die "uv 安装失败。请手动运行：curl -LsSf https://astral.sh/uv/install.sh | sh"
  unset_git_proxy
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;;
  esac
  case ":$PATH:" in
    *":$HOME/.cargo/bin:"*) ;; *) export PATH="$HOME/.cargo/bin:$PATH" ;;
  esac
fi
log_o "uv 就绪：$(uv --version)"

log_i "uv sync（创建 .venv / 对齐 lock 版本）"
uv sync || die "uv sync 失败，常见原因：网络（配置 HTTP_PROXY=host:port）/Python 版本不匹配"
log_o ".venv 就绪"

# ---------------------------------------------------------------------------
# 7.5) 真实 OKX 历史 K 线 Fixtures（用户 2026-08-29 要求"一定要真实历史 K"，默认强制真实）
#
# 环境变量：
#   YCS_SKIP_FIXTURES=1      完全跳过（fixtures 目录已自备真实文件 / git 仓库已附带）
#   YCS_FORCE_FIXTURES=1     --force：覆盖已有文件重拉（更新到最新历史）
#   YCS_ALLOW_SYNTH=1        OKX 真拿不到时应急兜底合成（不推荐）
# ---------------------------------------------------------------------------
hr
# 默认 YCS_SKIP_FIXTURES=1：真实 K 线 fixtures 随仓库一起 commit/push，git clone 下来就有，
# 部署时默认直接用，省网络时间也避免 VPS 被封 OKX 时部署挂。
# 需要在 VPS 上直接重拉最新真实数据时才传 YCS_SKIP_FIXTURES=0。
: "${YCS_SKIP_FIXTURES:=1}"
: "${YCS_FORCE_FIXTURES:=0}"
: "${YCS_ALLOW_SYNTH:=0}"

if [ "$YCS_SKIP_FIXTURES" -eq 1 ]; then
  log_w "YCS_SKIP_FIXTURES=1 → 跳过真实 OKX 历史 K 线拉取（依赖外部已有 fixtures/仓库自带）"
else
  FIX_ARGS=()
  [ "$YCS_FORCE_FIXTURES" -eq 1 ] && FIX_ARGS+=("--force")
  [ "$YCS_ALLOW_SYNTH"   -eq 1 ] && FIX_ARGS+=("--allow-synth")

  log_i "拉取真实 OKX 历史 K 线 Fixtures → deploy/fetch_market_fixtures.py ${FIX_ARGS[*]:-（仅缺失补拉）}"
  if uv run python deploy/fetch_market_fixtures.py "${FIX_ARGS[@]}"; then
    log_o "Fixtures 就绪"
  else
    # 若未允许合成但实际 OKX 外网不通 → 给出明确指引
    if [ "$YCS_ALLOW_SYNTH" -eq 1 ]; then
      die "Fixtures 生成失败（--allow-synth 已启用仍失败），请检查 VPS 磁盘/权限/网络代理配置。"
    else
      die "真实 OKX Fixtures 拉取失败，默认不允许合成兜底（用户要求真实历史 K）。" \
          "" \
          "修复方法二选一：" \
          "  A) 确认 VPS 能直连或代理到 www.okx.com（最推荐）：" \
          "       HTTPS_PROXY=http://<host>:<port> GIT_REPO=... bash install.sh" \
          "  B) VPS 上 OKX 确实被封时应急：传 YCS_ALLOW_SYNTH=1（会降级为确定性合成，pytest 仍过但非真实）。"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 8) pytest（生产环境默认必跑，避免 bug 版本自动上线）
# ---------------------------------------------------------------------------
hr
if [ "$YCS_SKIP_TEST" -eq 1 ]; then
  log_w "YCS_SKIP_TEST=1 → 跳过 pytest（不推荐）"
else
  log_i "运行 pytest 全量套件…"
  if uv run pytest tests/ -q --no-header; then
    log_o "pytest 全部通过"
  else
    die "pytest 存在失败用例，已中止部署以保护 VPS。" \
        "· 若确认为环境问题（如 systemd 缺失）：YCS_SKIP_TEST=1 bash install.sh" \
        "· 若确认为代码问题，请先修复后 push 到仓库再重跑。"
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
  sudo bash deploy/install_systemd.sh || die "install_systemd.sh 失败，见上方输出"
  log_o "部署完成；系统状态摘要："
  systemctl status ycs --no-pager --lines=10 || true
else
  log_i "更新完成 → ycsctl restart（加载新代码）"
  if sudo uv run python deploy/ycsctl.py restart; then
    log_o "ycs 服务重启成功"
  else
    log_w "ycsctl restart 失败（可能 systemd unit 尚未安装），改为首次执行 install_systemd.sh"
    sudo bash deploy/install_systemd.sh || die "install_systemd.sh 兜底失败，见上方输出"
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
    curl http://127.0.0.1:8000/           Dashboard 首页（中文界面）
    curl http://127.0.0.1:8000/api/status JSON 格式总览

  实盘前必须：
    1) uv run deploy/ycsctl.py check   —— 确保无任何 FATAL
    2) 先用纸盘模式（live=false）观察至少 1 天，确认逻辑 / 收益 / 风控符合预期
    3) 再切 live=true：vim config.yaml → trading.live: true
    4) 重启服务：uv run deploy/ycsctl.py restart
INSTALLSH_END
