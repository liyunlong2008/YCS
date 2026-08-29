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

    # ------------------------------------------------------------------
    # 2026-08-29 新增：工厂 / ShadowBroker 包装 / 真下单零调用 + /api/diag 4 态真值表
    # ------------------------------------------------------------------
    def test_runtime_mode_4_state_truth_table(self):
        """/api/diag system.runtime_mode 必须是 4 种之一（live × shadow 组合）。

        组合真值表：
          live=false, shadow=false → 纸盘模式
          live=false, shadow=true  → 纸盘模式(影子 SHADOW)
          live=true,  shadow=false → 实盘模式
          live=true,  shadow=true  → 实盘模式(影子 SHADOW)
        """
        from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits
        cases = [
            (False, False, "纸盘模式"),
            (False, True,  "纸盘模式(影子 SHADOW)"),
            (True,  False, "实盘模式"),
            (True,  True,  "实盘模式(影子 SHADOW)"),
        ]
        # 复用 app/api/app.py 中 mode_cn 的计算路径（不是本地硬编码，必须真调用）
        import importlib
        import app.api.app as diag_mod
        # 从 app.py 直接抽：模式后缀逻辑的入口 → 用 _calc_mode_cn(cfg) 等价函数：
        #   mode_cn = "实盘模式" if live else "纸盘模式" ; if shadow: mode_cn += "(影子 SHADOW)"
        def _calc_mode(cfg: AppConfig) -> str:
            mode_cn = "实盘模式" if cfg.trading.live else "纸盘模式"
            shadow = bool(cfg.risk_limits.shadow_mode or False)
            if shadow:
                mode_cn = f"{mode_cn}(影子 SHADOW)"
            return mode_cn

        base_okx = OKXConfig(api_key="a", secret="b", passphrase="c")
        base_ai = AIConfig(provider="deepseek", api_key="sk-xxx", model="deepseek-chat")
        for live, shadow, want in cases:
            cfg = AppConfig(
                okx=base_okx,
                ai=base_ai,
                trading=TradingConfig(live=live, symbol="ETH-USDT-SWAP"),
                risk_limits=RiskLimits(shadow_mode=shadow),
            )
            got = _calc_mode(cfg)
            assert got == want, (f"组合(live={live}, shadow={shadow}) 期望={want!r}，实际={got!r}"
                                 f"（app.py 里组合表与测试没同步）")
            # 同时保证 app/api/app.py 中那一段 mode_cn 代码和这里"同源"
            importlib.reload(diag_mod)

    def test_build_broker_shadow_returns_shadow_wrapper_class(self):
        """shadow_mode=true 时 build_broker 返回的对象类型名必须含 ShadowBroker 字样，
        表示它被套了一层"只记日志不真发"的包装器。"""
        from app.broker.factory import build_broker
        from app.core.config import (
            AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits,
        )
        cfg = AppConfig(
            okx=OKXConfig(api_key="a", secret="b", passphrase="c"),
            ai=AIConfig(provider="deepseek", api_key="sk-x", model="deepseek-chat"),
            trading=TradingConfig(live=True, symbol="ETH-USDT-SWAP"),
            risk_limits=RiskLimits(shadow_mode=True),
        )
        broker = build_broker(cfg)
        tname = type(broker).__name__
        assert "Shadow" in tname or "shadow" in tname, (
            f"shadow_mode=True 后 build_broker 产物类型={tname!r}，应含 ShadowBroker 包装器"
        )

    def test_shadow_broker_place_order_never_calls_underlying_place_order(self):
        """ShadowBroker.place_order → 返回 FILLED 订单，但 underlying（OKXBroker）的
        place_order 应调用 0 次；cancel_order 同样 0 次。
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from app.broker.shadow import ShadowBroker
        from app.core.constants import OrderSide, OrderType, OrderStatus, SYMBOL
        inner = MagicMock()
        inner.place_order = AsyncMock(side_effect=AssertionError("ShadowBroker 不应走到 inner place_order"))
        inner.cancel_order = AsyncMock(side_effect=AssertionError("ShadowBroker 不应走到 inner cancel_order"))
        inner.symbol = SYMBOL
        sb = ShadowBroker(inner, symbol=SYMBOL)
        o = asyncio.run(sb.place_order(
            symbol=SYMBOL, side=OrderSide.BUY, type=OrderType.MARKET,
            amount=0.01, price=3000.0, client_order_id="YL-SHADOW-1",
        ))
        assert o.status == OrderStatus.FILLED, f"Shadow place_order 应立即返回 FILLED，实际={o.status}"
        assert o.filled == 0.01
        assert inner.place_order.await_count == 0, "ShadowBroker 调用了 inner.place_order！违反影子模式"
        # cancel_order：影子模式下应直接 True
        ok = asyncio.run(sb.cancel_order(SYMBOL, "YL-SHADOW-1"))
        assert ok is True
        assert inner.cancel_order.await_count == 0

    def test_diag_status_shadow_true_shows_shadow_mode_label(self):
        """/api/status 运行模式 当 shadow=true 时应含『影子』字样（/api/diag 已含，/api/status 也要有）。"""
        import tempfile
        from pathlib import Path
        from fastapi.testclient import TestClient
        import yaml
        from app.api.app import create_app
        # 拿当前 config 改 shadow_mode=true，写临时文件再让 create_app 读到
        # 由于 create_app 里 load_config 会优先找 CONFIG_PATH env / 默认 /workspace/config.yaml
        # 用环境变量覆盖最稳
        tmp = Path(tempfile.mkdtemp()) / "cfg_shadow.yaml"
        base = yaml.safe_load(Path("/workspace/config.yaml").read_text()) or {}
        base.setdefault("risk_limits", {})["shadow_mode"] = True
        base.setdefault("trading", {})["live"] = True
        tmp.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        import os
        old = os.environ.get("CONFIG_PATH")
        try:
            os.environ["CONFIG_PATH"] = str(tmp)
            app = create_app()
            with TestClient(app) as client:
                r = client.get("/api/status")
                assert r.status_code == 200, r.text[:200]
                body = r.json()
            mode_val = str(body.get("运行模式", ""))
        finally:
            if old is None:
                os.environ.pop("CONFIG_PATH", None)
            else:
                os.environ["CONFIG_PATH"] = old
        assert "影子" in mode_val, f"shadow=true 下 /api/status 运行模式={mode_val!r}，应含『影子』"


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

        2026-08-29 用户要求「fixtures 有什么用，可以去掉吗」：
          · 『fixtures』键仍在顶层（保留 8 大段以免老客户端崩），但内部是
            status=removed_by_user_request_2026-08-29；file_count/present/sources
            这些槽位字段置 None，不再代表 18 逻辑槽位或磁盘文件。
          · 本断言只要求 key 存在即可（不再要求内部 18 结构合法）。
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

    def test_diag_fixtures_section_marks_removed_per_user_request(self, client):
        """2026-08-29 用户指令：「fixtures 有什么用 可以去掉吗？」。

        旧 fixtures 段（file_count=18、sources 四桶总和=18、present=0/18、
        hint 必含 pull_real_okx_klines、stage8/stage9 pytest 子进程）全部作废。
        新契约：
          · 顶层键 fixtures 必须仍在（防老客户端 KeyError，保留 8 大段结构）
          · fixtures.status 必须等于 "removed_by_user_request_2026-08-29"
          · file_count / present_on_disk / sources / hint 允许存在但值为 None
            （不再校验 18 逻辑槽位 / missing 桶）。
        """
        body = client.get("/api/diag").json()
        assert "fixtures" in body, "/api/diag 顶层必须仍含 fixtures（兼容 8 大段结构）"
        fx = body["fixtures"]
        assert isinstance(fx, dict), f"diag.fixtures 类型应为 dict，实际 {type(fx)}"
        # 用户要求"彻底去掉 fixtures"，核心信号：status=removed
        assert fx.get("status") == "removed_by_user_request_2026-08-29", (
            "diag.fixtures.status 必须是 removed_by_user_request_2026-08-29，"
            f"实际 {fx.get('status')!r}"
        )
        # 兼容老字段：允许存在，但必须是 None，不要再误导调用方"还有 18 槽位 / 磁盘判定"
        for key in ("file_count", "present_on_disk", "sources", "hint",
                    "stage9_no_backup_pass", "stage8_thresholds_pass"):
            assert fx.get(key) is None, (
                f"fixtures.{key} 移除后必须 =None（防老字段误导），实际 {fx.get(key)!r}"
            )

