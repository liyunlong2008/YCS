"""
TDD 阶段 10 · 实盘 7 条硬风控 + /api/diag 诊断快照接口（含 ycsctl kill 子命令）。
用户账户 14.8 USDT → 所有默认阈值都按超小仓位保守调。
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


# ============================================================================
# A1. 本金上限硬锁（live_max_equity_usdt）+ 适配 14.8U 默认值
# ============================================================================
class Test_A1_EquityCap:
    def test_config_yaml_has_new_risk_limits_section_and_14dot8_defaults(self):
        """config.yaml 必须新增 risk_limits 段，默认适配用户当前 14.8 USDT：
           - live_max_equity_usdt = 15.0 （略大于 14.8，覆盖充值后的全部余额）
           - live_max_daily_loss_usdt = 3.0
           - live_max_single_order_usdt = 2.0
        """
        import yaml
        cfg = yaml.safe_load(Path("/workspace/config.yaml").read_text())
        assert "risk_limits" in cfg, "config.yaml 缺少 risk_limits 段（用户要求按护栏方案补齐）"
        rl = cfg["risk_limits"]
        assert float(rl["live_max_equity_usdt"]) == 15.0, (
            f"live_max_equity_usdt 应默认 15.0（适配当前 14.8U 账户），实际 {rl.get('live_max_equity_usdt')}"
        )
        assert float(rl["live_max_daily_loss_usdt"]) == 3.0, (
            f"live_max_daily_loss_usdt 应默认 3.0（14.8U 单日最大 20% 亏损），实际 {rl.get('live_max_daily_loss_usdt')}"
        )
        assert float(rl["live_max_single_order_usdt"]) == 2.0, (
            f"live_max_single_order_usdt 应默认 2.0（单笔 ≤ 总资产 ~13%），实际 {rl.get('live_max_single_order_usdt')}"
        )
        assert "kill_switch_token" in rl, "缺少 kill_switch_token（/api/kill 的鉴权 Token）"
        # shadow 模式开关：应存在，默认 false（先不影子，用户说直接实盘但仍给开关位）
        assert "shadow_mode" in rl, "缺少 shadow_mode（影子模式：校验链路但不真下单）"

    def test_app_config_loads_risk_limits_pydantic(self):
        """AppConfig 必须能解析 risk_limits 段（新增 RiskLimits 模型）。"""
        from app.core.config import load_config
        cfg = load_config("/workspace/config.yaml")
        assert hasattr(cfg, "risk_limits"), "AppConfig 缺少 risk_limits 字段"
        rl = cfg.risk_limits
        assert float(rl.live_max_equity_usdt) == 15.0
        assert float(rl.live_max_daily_loss_usdt) == 3.0
        assert float(rl.live_max_single_order_usdt) == 2.0


# ============================================================================
# A2. 每日亏损熔断（按 USDT 绝对值，比百分比更稳，适合 14.8U 小账户）
# ============================================================================
class Test_A2_DailyLossHalt:
    def test_risk_engine_supports_absolute_usdt_daily_loss_limit(self):
        """RiskEngine 应新增 check_absolute_daily_loss(total, realized, unrealized) -> bool, reason。
           当 realized+unrealized <= -live_max_daily_loss_usdt → 触发 HALT。
        """
        from app.core.config import load_config
        from app.risk.engine import RiskEngine
        cfg = load_config("/workspace/config.yaml")
        re = RiskEngine()
        re.daily_start_balance = 14.8
        ok, reason = re.check_absolute_daily_loss(
            total_now=11.8, realized_pnl_usdt=-2.0, unrealized_pnl_usdt=-1.0,
            limit_usdt=cfg.risk_limits.live_max_daily_loss_usdt,
        )
        assert ok is False, f"-3.0 U 应触发日损熔断，实际 ok={ok} reason={reason}"
        assert "3.0" in reason or "日" in reason

    def test_daily_loss_within_limit_still_allowed(self):
        """-2.9 U 应仍允许（阈值 3.0）。"""
        from app.risk.engine import RiskEngine
        re = RiskEngine()
        re.daily_start_balance = 14.8
        ok, reason = re.check_absolute_daily_loss(
            total_now=11.9, realized_pnl_usdt=-1.9, unrealized_pnl_usdt=-1.0,
            limit_usdt=3.0,
        )
        assert ok is True, f"-2.9 U 未过阈值 3.0，应仍允许，实际 ok={ok} reason={reason}"


# ============================================================================
# A3. 订单大小双因子 sanity check
# ============================================================================
class Test_A3_OrderSanity:
    def test_order_sanity_rejects_too_big_single_order(self):
        """单笔名义价值超过 live_max_single_order_usdt → 拒绝。"""
        from app.core.safety import order_size_sanity_check
        rej, reason = order_size_sanity_check(
            qty_contracts=1.0,            # ETH 合约 1 张，价格 2000 → 名义 2000（远超过 2）
            last_price=2000.0,
            total_equity=14.8,
            max_single_usdt=2.0,
            position_change_pct=0.10,     # 10% 本没问题，但单笔就超了
        )
        assert rej is True, f"单笔名义 2000U > 2U 上限应拒绝，实际 rej={rej} reason={reason}"
        assert "单笔" in reason or "名义" in reason

    def test_order_sanity_rejects_large_position_change(self):
        """从 0 直接加到 50% 总资产 → 虽单笔未超 2U，但变动率 > 10% → 拒绝。"""
        from app.core.safety import order_size_sanity_check
        rej, reason = order_size_sanity_check(
            qty_contracts=0.002,          # 名义 4U？不对，14.8U 总资产 10% = 1.48U；4U > 1.48U
            last_price=2000.0,            # 名义 = 0.002 * 2000 = 4.0 U
            total_equity=14.8,
            max_single_usdt=5.0,          # 单笔 4 没问题
            position_change_pct=0.10,     # 变动率上限 10% = 1.48U
        )
        assert rej is True, f"仓位变动 4U/14.8U ≈ 27% > 10% 应拒绝，实际 rej={rej} reason={reason}"
        assert "变动率" in reason or "仓位" in reason


# ============================================================================
# A4. 真实仓位对账（Position Reconciliation）
# ============================================================================
class Test_A4_PositionReconciliation:
    def test_reconcile_detects_size_mismatch_returns_halt(self):
        """本地 PM 认为 size=0，交易所返回 size=0.01（影子仓位）→ 对账返回 halt=True。"""
        from app.core.safety import reconcile_position
        from app.broker.base import Position
        from app.core.constants import PositionSide
        local = Position(symbol="ETH-USDT-SWAP", side=PositionSide.FLAT, size=0.0)
        exchange = Position(symbol="ETH-USDT-SWAP", side=PositionSide.LONG, size=0.01,
                            entry_price=2000, mark_price=2000, unrealized_pnl=0, leverage=1,
                            liquidation_price=0)
        halt, reason = reconcile_position(local, exchange, tolerance_usdt=0.5)
        assert halt is True, f"size 0 vs 0.01 应触发对账 HALT，实际 halt={halt} reason={reason}"
        assert "不一致" in reason or "对账" in reason

    def test_reconcile_within_tolerance_returns_ok(self):
        """两边 size 都 0 → OK。"""
        from app.core.safety import reconcile_position
        from app.broker.base import Position
        from app.core.constants import PositionSide
        a = Position(symbol="ETH-USDT-SWAP", side=PositionSide.FLAT, size=0.0)
        b = Position(symbol="ETH-USDT-SWAP", side=PositionSide.FLAT, size=0.0)
        halt, reason = reconcile_position(a, b, tolerance_usdt=0.5)
        assert halt is False, f"两边空仓应 OK，实际 halt={halt} reason={reason}"


# ============================================================================
# A5. Kill-Switch 三通道（ycsctl kill 子命令存在 + /api/kill 路由 + EMERGENCY_HALT 文件）
# ============================================================================
class Test_A5_KillSwitchThreeChannels:
    def test_ycsctl_has_kill_subcommand(self):
        """ycsctl --help 应出现 'kill' 子命令。"""
        r = os.popen("cd /workspace && uv run python deploy/ycsctl.py --help 2>&1 | grep -E '^  kill|kill.*紧急'").read()
        assert "kill" in r.strip(), f"ycsctl 缺少 kill 子命令，grep 结果={r!r}"

    def test_api_app_exposes_kill_and_diag_routes(self):
        """FastAPI create_app() 返回的 app 路由表中应存在 /api/kill 和 /api/diag。"""
        from app.api.app import create_app
        app = create_app()
        paths = {r.path for r in app.routes}
        assert "/api/kill" in paths, "缺少 POST /api/kill kill-switch 路由"
        assert "/api/diag" in paths, "缺少 GET /api/diag 诊断快照接口"

    def test_emergency_halt_file_function_exists(self):
        """app.core.safety 应提供 check_emergency_halt_file(rt_path='/workspace/data/EMERGENCY_HALT')。
           存在文件 → 返回 (True, reason)；不存在 → (False, '')。
        """
        from app.core.safety import check_emergency_halt_file
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "EMERGENCY_HALT"
            ok, _ = check_emergency_halt_file(p)
            assert ok is False
            p.write_text("halt", encoding="utf-8")
            ok, reason = check_emergency_halt_file(p)
            assert ok is True and "EMERGENCY_HALT" in reason


# ============================================================================
# A6. 下单幂等键（生成 ycs_<epoch_ms>_<rand8> + 校验格式）
# ============================================================================
class Test_A6_IdempotentClientOrderId:
    def test_generate_client_order_id_format_and_uniqueness(self):
        from app.core.safety import generate_client_order_id
        ids = [generate_client_order_id() for _ in range(1000)]
        assert len(set(ids)) == 1000, "1000 个幂等键不应有重复"
        sample = ids[0]
        assert sample.startswith("ycs_"), f"幂等键前缀应为 ycs_，实际 {sample}"
        parts = sample.split("_")
        assert len(parts) == 3, f"格式应为 ycs_<ms>_<rand8>，实际 {sample}"
        assert parts[1].isdigit() and len(parts[1]) >= 12
        assert len(parts[2]) == 8


# ============================================================================
# A7. shadow 影子模式（Broker 包装：生成请求但拦截实际发单，返回模拟成功结果）
# ============================================================================
class Test_A7_ShadowMode:
    def test_risk_limits_shadow_mode_reads_from_config(self):
        """AppConfig.risk_limits.shadow_mode=False 默认存在。"""
        from app.core.config import load_config
        cfg = load_config("/workspace/config.yaml")
        assert isinstance(cfg.risk_limits.shadow_mode, bool)

    def test_safety_has_shadow_gate_function(self):
        """should_block_real_orders(mode, token_present) 依据 shadow_mode 返回 True/False。"""
        from app.core.safety import should_block_real_orders
        # shadow=True → 必须拦截实盘真发单
        assert should_block_real_orders(shadow_mode=True) is True
        # shadow=False → 不拦截（交给后续 live=true + safety 其他护栏）
        assert should_block_real_orders(shadow_mode=False) is False


# ============================================================================
# B. GET /api/diag 诊断快照接口（返回结构化数据，供 AI 后续分析缺陷）
# ============================================================================
class Test_B_DiagnosticSnapshotAPI:
    @pytest.fixture
    def client(self):
        from app.api.app import create_app
        from fastapi.testclient import TestClient
        # 构造最小 runtime：Controller 为空（仅保证路由可用）
        app = create_app()
        return TestClient(app)

    def test_diag_returns_200_and_top_level_keys(self, client):
        """GET /api/diag 应返回 200，顶层至少包含 8 大类：
           system, broker, controller, position_manager, journal,
           safety, fixtures, risks（自动缺陷检测 Top 3 警告）。
        """
        r = client.get("/api/diag")
        assert r.status_code == 200, f"/api/diag HTTP {r.status_code}（应 200）: {r.text[:200]}"
        body = r.json()
        required = {"system", "broker", "controller", "position_manager",
                    "journal", "safety", "fixtures", "risks"}
        missing = required - set(body.keys())
        assert not missing, f"/api/diag 顶层缺少键: {missing}"

    def test_diag_system_section_has_essential_fields(self, client):
        """system 至少含 runtime_mode（纸盘/实盘/影子）、started_at、pid、version、
           live_max_equity_usdt、live_max_daily_loss_usdt。
        """
        body = client.get("/api/diag").json()
        sys_sec = body["system"]
        for k in ("runtime_mode", "version", "live_max_equity_usdt", "live_max_daily_loss_usdt"):
            assert k in sys_sec, f"diag.system 缺少 {k}"

    def test_diag_risks_section_is_list_of_strings(self, client):
        """risks 应为 str 列表（自动缺陷检测警告 Top 3）。占位模式下至少应含：
           ① OKX key 仍是占位值（若未配置真实 key）或 ② AI key 仍占位 警告之一。
        """
        body = client.get("/api/diag").json()
        risks = body["risks"]
        assert isinstance(risks, list), f"diag.risks 类型应为 list[str]，实际 {type(risks)}"
        assert all(isinstance(x, str) for x in risks), "risks 每项必须是字符串"
        # 占位配置下应至少检测出 1 条风险（key 占位）
        assert len(risks) >= 1, "占位配置下 diag.risks 至少 1 条警告（key 占位）"

    def test_diag_fixtures_section_contains_stage9_and_stage8_status(self, client):
        """fixtures 段应含：file_count（18）、sources（real/synth/missing 统计）、
           stage9_no_backup_pass（bool）、stage8_thresholds_pass（bool）四项。
        """
        body = client.get("/api/diag").json()
        fx = body["fixtures"]
        for k in ("file_count", "sources", "stage9_no_backup_pass", "stage8_thresholds_pass"):
            assert k in fx, f"diag.fixtures 缺少 {k}，实际 keys={list(fx.keys())}"
        assert int(fx["file_count"]) == 18, f"fixtures file_count 应为 18，实际 {fx['file_count']}"
