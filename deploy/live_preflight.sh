#!/usr/bin/env bash
# =============================================================================
# 云龙挑战赛（YCS）切实盘前最终预检脚本 — deploy/live_preflight.sh
#
# 用法（VPS 上直接跑）:
#   cd /opt/ycs && bash deploy/live_preflight.sh
#
# 结论：
#   · PASS_COUNT / TOTAL_COUNT == 10 / 10 → 可以切 shadow_mode=false 切实盘
#   · 任何 FATAL → 先修再切
#
# 注意：本脚本"只读"，不会改 config / 发订单 / 碰真实交易所。
# =============================================================================
set -euo pipefail
PROG=$(basename "$0")
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
pass=0; fail=0; total=0

# ---- 工作目录推导 ----
cd "$(dirname "$0")/.."   # deploy/*.sh → 项目根
ROOT=$PWD
echo "============================================================"
echo "  云龙挑战赛 · 切实盘前最终预检"
echo "  项目根 : $ROOT"
echo "  时间   : $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
[ -f "$ROOT/config.yaml" ] || { echo "${RED}FATAL: 找不到 $ROOT/config.yaml${RESET}" >&2; exit 1; }
UV="uv run"
PY=( $UV python )

chk() { local name="$1"; shift; total=$((total+1));
  echo -n "  [$total/10] $name  …  "
  if "$@" >/dev/null 2>&1; then echo "${GREEN}PASS${RESET}"; pass=$((pass+1));
  else echo "${RED}FAIL${RESET}"; fail=$((fail+1)); fi
}

SUMMARY=""
note() { SUMMARY+="   · $*"$'\n'; }

# -----------------------------------------------------------------------------
# ① 配置自检：ycsctl check — 不能有 FATAL，影子/实盘按当前配置走
# -----------------------------------------------------------------------------
chk "① ycsctl check 无 FATAL" \
    "$PY" deploy/ycsctl.py check
FATAL_COUNT=$( "$PY" deploy/ycsctl.py check 2>&1 | grep -cE "^ *FATAL" || true )
if [ "$FATAL_COUNT" -gt 0 ]; then
  note "① ycsctl check 仍有 FATAL=$FATAL_COUNT：先修配置（通常是 OKX API key/AI key 占位）"
else
  note "① 配置自检：通过（占位密钥会被 ycsctl check 用 [WARN] 提示但不计 FATAL）"
fi

# -----------------------------------------------------------------------------
# ② pytest 风控/诊断/控制器冒烟子集
# -----------------------------------------------------------------------------
chk "② pytest 风控子集（~150 case）" \
    bash -lc "cd '$ROOT' && uv run pytest tests/ -q \
        --ignore=tests/test_stage3_klines.py \
        --ignore=tests/test_stage8_market_fixtures.py \
        --ignore=tests/test_stage9_real_klines_integration.py 2>&1 | tail -3 | grep -qE '^[0-9]+ passed'"
PASS_LINES=$(bash -lc "cd '$ROOT' && uv run pytest tests/ -q \
    --ignore=tests/test_stage3_klines.py \
    --ignore=tests/test_stage8_market_fixtures.py \
    --ignore=tests/test_stage9_real_klines_integration.py 2>&1 | tail -2 | grep -oE '^[0-9]+ passed' || true")
note "② pytest 子集：${PASS_LINES:-'0 passed'}（第三方 DeprecationWarning 可忽略）"

# -----------------------------------------------------------------------------
# ③ Dashboard 可访问（首页 HTTP 200 + 含 "Dashboard"）
# -----------------------------------------------------------------------------
PORT=$( "$PY" -c "
import yaml,sys
cfg=yaml.safe_load(open('$ROOT/config.yaml'))
sc=cfg.get('server') or {}
print(int(sc.get('port',8765)))
")
chk "③ Dashboard / 首页 HTTP 200（端口 $PORT）" \
    bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 http://127.0.0.1:$PORT/); [ \"\$code\" = 200 ]"
note "③ Dashboard 端口=$PORT（对外 http://<公网IP>:$PORT/ ）"

# -----------------------------------------------------------------------------
# ④ 余额 ≥ 最小名义（否则 100% 下单失败，直接拦）：balance_total * leverage × 0.5 > min_notional
# -----------------------------------------------------------------------------
chk "④ 余额 ≥ 最小名义：能下到 OKX min sz（≈2.4U @ ETH 现价）" \
    bash -lc "cd '$ROOT' && $UV python - <<'PYEOF' >/dev/null 2>&1
import yaml
cfg = yaml.safe_load(open('config.yaml'))
# 真拉 OKX / broker 当前余额快照：走健康检查 endpoint /api/balance
import urllib.request, json
port = (cfg.get('server') or {}).get('port', 8765)
b = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/balance', timeout=8))
bal = float(b.get('balance_total', 0) or 0)
# 粗略：ETH @ ~2500 → ct_val=0.01 → min sz 0.1 → min_notional ≈ 2.5U
min_notional = 2.5
lev = int(((cfg.get('trading') or {}).get('default_leverage') or 1))
# 至少让『最小名义』有 2x 余量才开实盘（避免一次就打光）
exit(0 if (bal * lev >= min_notional * 2) else 1)
PYEOF"
BAL_INFO=$(bash -lc "cd '$ROOT' && $UV python - <<'PYEOF' 2>/dev/null || true
import yaml, urllib.request, json
cfg = yaml.safe_load(open('config.yaml'))
port = (cfg.get('server') or {}).get('port', 8765)
try:
    b = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/balance', timeout=8))
    bal = float(b.get('balance_total', 0) or 0); av = float(b.get('available', 0) or 0)
    lev = int(((cfg.get('trading') or {}).get('default_leverage') or 1))
    print(f'余额={bal:.3f}U 可用={av:.3f}U 杠杆={lev}X → 名义上限≈{bal*lev:.2f}U')
except Exception as e:
    print(f'查询失败: {e}')
PYEOF")
note "④ 余额&杠杆：${BAL_INFO:-'？'}（要求最小名义 2.5U 有 2×余量）"

# -----------------------------------------------------------------------------
# ⑤ 影子模式安全闸：当前如果仍是 shadow_mode=True → 一定不会真发（ycsctl check 已验证，这里再贴 runtime_mode 截图）
# -----------------------------------------------------------------------------
chk "⑤ /api/status runtime_mode 字段正确存在" \
    bash -lc "cd '$ROOT' && curl -s --max-time 6 http://127.0.0.1:$PORT/api/status \
        | python -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if \"运行模式\" in d else 1)' >/dev/null 2>&1"
MODE=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/status | python -c 'import sys,json; d=json.load(sys.stdin); print(d.get(\"运行模式\",\"?\"))'")
note "⑤ 运行模式：${MODE:-?}（切真前必须先观察≥6h 影子模式）"

# -----------------------------------------------------------------------------
# ⑥ daily_start_balance 合理性：14.83U 账户决不能再出现 1000U → 假熔断
# -----------------------------------------------------------------------------
chk "⑥ daily_start_balance 合理（/api/diag ratio 在 0.7~1.3 之间）" \
    bash -lc "cd '$ROOT' && curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python - <<'PYEOF' >/dev/null 2>&1
import sys, json
d = json.load(sys.stdin)
ratio = float(d['controller']['risk']['daily_start_vs_cur_ratio'])
sys.exit(0 if 0.7 <= ratio <= 1.3 else 1)
PYEOF"
SANITY=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python -c 'import sys,json; d=json.load(sys.stdin); print(d[\"controller\"][\"risk\"].get(\"sanity_status\",\"?\"))'")
note "⑥ daily_start 合理性：${SANITY:-？}（正常=日内盈亏范围内；异常=自动修正或余额突变未刷新）"

# -----------------------------------------------------------------------------
# ⑦ 风控『最近一次结论』字段齐全（Dashboard 能解释为什么不开仓）
# -----------------------------------------------------------------------------
chk "⑦ /api/diag last_risk_* 字段齐全" \
    bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python - <<'PYEOF' >/dev/null 2>&1
import sys, json
d = json.load(sys.stdin); s = d['system']
need = ['last_risk_conclusion','last_risk_reason','last_risk_suggested_notional_usdt','last_risk_min_notional_usdt']
sys.exit(0 if all(s.get(k) is not None for k in need) else 1)
PYEOF"
REASON=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python -c 'import sys,json;d=json.load(sys.stdin);print(d[\"system\"].get(\"why_no_position\",\"?\"))'")
note "⑦ 最近风控『为什么不开仓』：${REASON:-？}"

# -----------------------------------------------------------------------------
# ⑧ 实时 mark 价格 ≥ 100（不是硬编码 0 → 最小名义不卡死）
# -----------------------------------------------------------------------------
chk "⑧ broker.position.mark_price ≥ 100（随 ETH 现价刷新）" \
    bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python - <<'PYEOF' >/dev/null 2>&1
import sys, json
d = json.load(sys.stdin); p = float(d['broker']['position']['mark_price'] or 0)
sys.exit(0 if p >= 100 else 1)
PYEOF"
MP=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python -c 'import sys,json;d=json.load(sys.stdin);print(d[\"broker\"][\"position\"][\"mark_price\"])'")
note "⑧ 当前标记价：${MP:-？}（应在 ~2400~2500，异常=真行情拉取失败）"

# -----------------------------------------------------------------------------
# ⑨ AI 最近一次结论非空（last_ai 至少有 regime + ts_ms，避免 AI 全崩了没发现）
# -----------------------------------------------------------------------------
chk "⑨ last_ai 最近一次结论存在（regime + 时间戳）" \
    bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python - <<'PYEOF' >/dev/null 2>&1
import sys, json, time as _t
d = json.load(sys.stdin); ai = d['controller'].get('last_ai') or {}
sys.exit(0 if (ai.get('regime') and int(ai.get('ts_ms') or 0) > int(_t.time()-3600)*1000) else 1)
PYEOF"
REGIME=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python -c 'import sys,json;ai=json.load(sys.stdin)[\"controller\"].get(\"last_ai\",{}); print(f\"{ai.get(\\\"regime\\\",\\\"?\\\")} conf={ai.get(\\\"confidence\\\",\\\"?\\\")}\")'")
note "⑨ 最近 AI：${REGIME:-？}（超时=AI 通道挂；HIGH_VOL=合理观望；TREND_CONF=趋势单即将出现）"

# -----------------------------------------------------------------------------
# ⑩ 强平价方向合理：若当前持仓 → LONG 强平价 < entry；SHORT 强平价 > entry（杜绝再次 4642 Bug）
# -----------------------------------------------------------------------------
chk "⑩ 持仓强平价方向合理（LONG<entry；SHORT>entry；FLAT=0）" \
    bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python - <<'PYEOF' >/dev/null 2>&1
import sys, json
d = json.load(sys.stdin); p = d['broker']['position']
side = p['side']; entry = float(p['entry_price'] or 0); liq = float(p.get('liquidation_price') or 0)
if side == 'FLAT':
    sys.exit(0)
elif side == 'LONG':
    sys.exit(0 if (0 < liq < entry) else 1)
elif side == 'SHORT':
    sys.exit(0 if (liq > entry) else 1)
else:
    sys.exit(1)
PYEOF"
POS=$(bash -lc "curl -s --max-time 6 http://127.0.0.1:$PORT/api/diag | python -c 'import sys,json;p=json.load(sys.stdin)[\"broker\"][\"position\"];print(f\"side={p[\\\"side\\\"]} sz={p[\\\"size\\\"]:.4f} entry={p[\\\"entry_price\\\"]:.2f} liq={p.get(\\\"liquidation_price\\\",0):.2f}\")'")
note "⑩ 当前持仓方向 & 强平价：${POS:-？}（再次确认 Bug #4642 已修复：空单 4642→2691）"

# -----------------------------------------------------------------------------
# 输出
# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  预检结果 : $pass / $total 通过（必须 10/10 再切实盘）"
echo "============================================================"
echo
echo "【项目说明】"
echo -n "$SUMMARY"
echo
if [ "$fail" -eq 0 ]; then
  echo "${GREEN}★ 所有检查通过：可按『切实盘 4 步』操作${RESET}"
  echo
  echo "  → 切实盘 4 步："
  echo "    1. cd $ROOT"
  echo "    2. ycsctl stop"
  echo "    3. vim config.yaml → risk_limits.shadow_mode: false
       * 同时确认 trading.live: true；OKX API Key/Secret/Passphrase 均为真实非占位值
       * 『第一手真单建议』先把 trading.default_leverage 压到 2X~3X 跑 12 小时，观察无异常再回 5X~10X"
  echo "    4. ycsctl start   (扫场闸门自动 cancel_all + close_all 残留，然后进入 RUNNING)"
  echo
  echo "  → 切实盘后第一小时监控："
  echo "    · 每 5 分钟 curl $PORT/api/diag 核对『余额 / 持仓 / 强平价 / daily_start』"
  echo "    · 有任何异常：curl -X POST -H 'X-YCS-Admin-Token: <配置里kill_switch_token>' http://127.0.0.1:$PORT/api/kill"
  exit 0
else
  echo "${RED}✗ 存在 $fail 项不通过 → 先按上面『项目说明』定位修复，再重跑 $PROG${RESET}"
  exit 2
fi
