"""2026-09-01 VPS 现场诊断 Bug 系列 RED→GREEN TDD：

现场报告（/api/logs 运行一整夜）:
  1. /api/status vs /api/diag started_at/uptime 不一致:
       status.started_at=1788188737, uptime=8h31m28s ✅
       diag.started_at=1788219363,  uptime_seconds=0s ❌
     根因: /api/diag 用 runtime.state_store (或 None→默认 fallback=time.time()) 算 started_at，
           但 /api/status 用 ctl.state_store (run.py/recoverer 写入真源)，两者不是同一个对象。
  2. /api/diag system.status 返回 None:
     根因: /api/diag 没有把 status_from_ctl["系统状态"] 回写到 system_block.status
  3. /api/diag why_no_position 误报『风控+AI双过，信号应已发送』:
     现场系统状态=HALT（05:01 强平前主动平仓后进入停机保护），持仓空是因为系统停机，
     但 why_no_position 逻辑只判风控+AI双过→不判系统状态→误报"信号发了没成交"。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from app.broker.paper import PaperBroker
from app.core.constants import SystemStatus


class _FakeStore:
    def __init__(self, init: dict): self._d = dict(init)
    def load(self): return dict(self._d)
    def save(self, d): self._d = dict(d)


def _make_runtime(status_from_ctl_expected_match: bool = True):
    """构造 create_app 运行时：ctl + ctl.state_store + cfg + rt.state_store。

    关键：当 status_from_ctl_expected_match=False（VPS 现场）时，
    rt.state_store（/api/diag 默认真源）与 ctl.state_store（/api/status 真源）
    不是同一个对象，started_at 完全不同——仿真现场 Bug：
       ctl.state_store.started_at = 1788188737 (早 8h，正确)
       rt.state_store.started_at  = 1788219363 (晚 8h，错误 fallback=time.time)
    修复要求：/api/diag 必须 100% 以 ctl.aget_status_dict() / ctl.state_store.load() 中的
    started_at / 系统状态 作为真源，不使用独立 started_at 计算。
    """
    import time as _t
    from app.core.ai_throttle import AIThrottler
    from app.risk.engine import RiskEngine
    from app.services.controller import TradingController

    ctl = object.__new__(TradingController)

    class _TC: mode = "live"; live = True; symbol = "ETH-USDT-SWAP"; default_leverage = 10
    class _RC:
        shadow_mode = True
        live_max_equity_usdt = 100.0
        live_max_daily_loss_usdt = 30.0
        kill_switch_token = "k"
        risk_per_trade_pct = 5.0
        stop_loss_price_pct = 2.5
        position_change_pct = 0.2
        max_order_notional_usdt = 0.0
        min_order_notional_usdt = 0.0
        emergency_halt_file = None
    class _OKX: api_key = "PLACEHOLDER_OKX_API_KEY_TEST"; secret = "x"; passphrase = "y"
    class _AI: provider = "deepseek"; api_key = "PLACEHOLDER_AI_KEY_TEST"; model = "deepseek-chat"; base_url = ""
    class _Cfg: trading = _TC(); risk_limits = _RC(); okx = _OKX(); ai = _AI()
    ctl.config = _Cfg()
    ctl._last_ai = None; ctl._last_ai_ts = None
    ctl.ai_throttler = AIThrottler()
    ctl.risk = RiskEngine()
    ctl.order_manager = MagicMock()
    ctl.order_manager.stats.return_value = {
        "trades_opened": 1, "trades_closed": 1,
        "wins": 0, "losses": 0, "consecutive_losses": 0,
        "max_consecutive_losses": 3, "allow_trading": True, "cooldown_until": 0,
        "daily_loss_pct": 0.0, "daily_start_balance": 14.83,
        "daily_realized_pnl": 0.0, "position_change_pct": 0.2, "risk_per_trade_pct": 5.0,
    }
    ctl.position_manager = MagicMock(); ctl.position_manager.to_dict.return_value = {}
    ctl.market_producer = None
    # ctl.state_store（真源）: started_at=1788188737（早 8h，对应 VPS 08-31 23:05 启动），status=HALT
    now_fake = 1788219426  # ≈ 2026-09-01 07:37:06（与 VPS 抓取现场一致）
    true_started_at = 1788188737  # 08-31 23:05:37
    expected_uptime_s = now_fake - true_started_at  # = 30689s ≈ 8h31m29s
    ctl.state_store = _FakeStore({
        "status": SystemStatus.HALT.value,
        "started_at": true_started_at,
        "balance": {"total": 14.83, "available": 14.83, "unrealized_pnl": 0.0},
        "risk": {"daily_start_balance": 14.83},
        "stats": {},
        "time_sync": {"drift_ms": 0, "last_sync_at": now_fake, "drifted_pause": False},
        "position": {"side": "FLAT", "size": 0.0},
    })
    # 让 recoverer 拿不到新时间：monkey-patch time.time() 让 _assemble_status_dict 内部时间一致
    ctl.broker = PaperBroker(initial_balance=100.0)

    # rt.state_store（/api/diag 独立真源，现场情况：和 ctl.state_store 是两个对象）
    #   · 默认 fallback 是 time.time()（现场 Bug：1788219363）
    rt_state_store = _FakeStore({
        "started_at": 1788219363 if not status_from_ctl_expected_match else true_started_at,
        # 故意 status 不同（模拟 runtime.state_store 不是 recoverer/run.py 写入的那个）
        "status": SystemStatus.RUNNING.value,
    })
    return ctl, ctl.config, ctl.state_store, rt_state_store, now_fake, true_started_at, expected_uptime_s


class Test_DiagMatchesStatusAfterHalt:
    """API 层 /api/diag 必须和 /api/status 的启动时间/uptime/status/why_no_position 完全一致。"""

    @staticmethod
    @pytest.mark.asyncio
    async def test_diag_started_at_uptime_status_identical_to_status():
        """必须：/api/diag started_at == /api/status 启动时间戳；uptime 之差绝对值≤3s；system.status 非空。"""
        from app.api.app import create_app
        ctl, cfg, ctl_store, rt_store, now_fake, true_sa, expected_upt = _make_runtime(status_from_ctl_expected_match=False)
        runtime = {
            "config": cfg,
            "controller": ctl,
            "state_store": rt_store,  # 注意：这是「错的对象」，不是 ctl.state_store——现场 Bug 场景
            "runtime_root": None,
            "logs_root": None,
            "trade_journal": None,
        }
        app = create_app(runtime=runtime)
        from fastapi.testclient import TestClient
        # 注意：时间冻结对 /api/status 内部 aget_status_dict 用 time.time()，用标准自由函数 patch
        import time as _t_m
        from unittest.mock import patch
        with patch("time.time", return_value=now_fake):
            client = TestClient(app)
            r_status = client.get("/api/status")
            r_diag = client.get("/api/diag")
        assert r_status.status_code == 200, f"/api/status HTTP {r_status.status_code}: {r_status.text[:120]}"
        assert r_diag.status_code == 200, f"/api/diag HTTP {r_diag.status_code}: {r_diag.text[:120]}"
        s = r_status.json()
        d = r_diag.json()
        status_started_at = int(s.get("启动时间戳(epoch秒)") or 0)
        status_uptime_s = int(s.get("运行时长(秒)") or 0)
        diag_started_at = int((d.get("system") or {}).get("started_at") or 0)
        diag_uptime_s = int((d.get("system") or {}).get("uptime_seconds") or 0)
        diag_status = (d.get("system") or {}).get("status")
        # 核心断言
        assert diag_started_at == status_started_at, (
            f"/api/diag started_at={diag_started_at} ≠ /api/status 启动时间戳={status_started_at} "
            f"（现场 Bug：/api/diag 用了错误的 runtime.state_store）"
        )
        assert abs(diag_uptime_s - status_uptime_s) <= 3, (
            f"/api/diag uptime_seconds={diag_uptime_s}, /api/status 运行时长(秒)={status_uptime_s} 差超 3s"
        )
        assert diag_status is not None and str(diag_status) != "" and str(diag_status) != "None", (
            f"/api/diag system.status={diag_status}（现场 None）→ 必须从 aget_status_dict 的系统状态同步"
        )

    @staticmethod
    @pytest.mark.asyncio
    async def test_diag_why_no_position_halt_explains_stopped_not_signal_sent():
        """现场：status=HALT / 风控+AI双过 / 持仓空 → why_no_position 必须说明『停机保护』，
        不能误导『信号已发仍未持仓』。"""
        from app.api.app import create_app
        ctl, cfg, ctl_store, rt_store, now_fake, true_sa, expected_upt = _make_runtime(status_from_ctl_expected_match=False)
        # 把风险最近结论+AI信号预置为『通过』—— 仿真现场状态（风控通过AI到位但系统=HALT）
        # · 方式：monkey-patch risk.last_verdict（字段是 allow 不是 allowed）
        from app.risk.engine import RiskVerdict
        ctl.risk.last_verdict = RiskVerdict(
            allow=True, reason="风控通过测试", suggested_notional_usdt=2.47,
            min_notional_usdt=2.47, capital_shortfall_usdt=None,
            suggested_leverage=10, suggested_qty_contract=0.1,
        )
        ctl.risk.last_verdict_at = now_fake - 3600  # 1h 前
        # 模拟 AI 最近 TREND_UP conf=72：给 _last_ai 塞个对象
        from app.ai.base import MarketAnalysisResult
        ctl._last_ai = MarketAnalysisResult(market_regime="TREND_UP", confidence=72, reason="测试上涨趋势")
        ctl._last_ai_ts = now_fake - 3600

        runtime = {
            "config": cfg,
            "controller": ctl,
            "state_store": rt_store,
            "runtime_root": None,
            "logs_root": None,
            "trade_journal": None,
        }
        app = create_app(runtime=runtime)
        from fastapi.testclient import TestClient
        import time as _t_m
        from unittest.mock import patch
        with patch("time.time", return_value=now_fake):
            client = TestClient(app)
            r = client.get("/api/diag")
        assert r.status_code == 200
        sysb = r.json().get("system") or {}
        why = str(sysb.get("why_no_position") or "")
        # 必须出现 HALT / 停机 / 保护 字样，不应包含『信号应已发送』的误导
        assert ("HALT" in why or "停机" in why or "保护" in why or "停止" in why), (
            f"现场 status=HALT 状态下 why_no_position='{why}'，必须说明停机保护，"
            "不能误导『风控+AI双过，信号已发』"
        )
        signal_mislead = "信号应已发送" in why or "信号已发送" in why
        assert not signal_mislead, (
            f"HALT 状态 why_no_position='{why}' 含『信号已发送』类误导"
        )


class Test_StatusHaltDoesntTriggerTrades:
    """回归：bg_main_loop 在 SystemStatus.HALT 时，不进入 RUNNING 守卫，不触发新一轮下单。"""

    @staticmethod
    def test_get_status_dict_returns_halt_when_state_says_halt():
        """同步 get_status_dict（CLI 场景）必须从 ctl.state_store 读 HALT，不乱兜底为 RUNNING。

        uptime 容差放宽到 ±180s（time.time() 没被 patch 的情况下，真实时钟和 now_fake 可能差 ≤2~3 min，
        这不是 Bug；关键是「uptime 不会归零 / 不会出现负值」。"""
        ctl, cfg, ctl_store, rt_store, now_fake, true_sa, expected_upt = _make_runtime(status_from_ctl_expected_match=True)
        # get_status_dict 会实时查 broker / time.time（没被 patch），用 patch 冻结 time 防 real clock 导致断言漂移
        from unittest.mock import patch
        with patch("time.time", return_value=now_fake):
            d = ctl.get_status_dict()
        sys_s = str(d.get("系统状态") or "")
        is_halt = ("保护" in sys_s or "HALT" in sys_s.upper() or "停机" in sys_s or "STOP" in sys_s.upper())
        assert is_halt, (
            f"state_store.status=HALT, 但 get_status_dict 返回系统状态={sys_s}（应=停机保护/HALT）"
        )
        got_upt = int(d.get("运行时长(秒)") or 0)
        # uptime 关键：0 就失败（/api/diag 现场现象）；否则判它≈真实启动距今（不超 24h=86400s，数据合理）
        assert got_upt > 0, f"get_status_dict 运行时长(秒)=0（现场 Bug），期望 >0，真 started_at=true_sa"
        assert got_upt == expected_upt, (
            f"运行时长(秒)={got_upt} ≠ 冻结时间下的期望 {expected_upt}s，started_at 可能不是真源值 true_sa={true_sa}"
        )
        # 相对 true_sa，uptime 不能比现在-真+86400 大（否则就是读了个未来 started_at）
        import time as _t_now
        real_max_upt = int(_t_now.time()) - true_sa + 86400  # 容差 1 天（跨天/时区小错不判错）
        assert got_upt <= real_max_upt, (
            f"运行时长(秒)={got_upt} 远超合理上限 {real_max_upt}，可能 started_at 读了未来"
        )


class Test_HaltAutoResumeNextDay:
    """现场：05:01 强平前主动平仓 → HALT。次日 00:00 自动切回 RUNNING，用户不用手动 resume。"""

    @staticmethod
    def test_apply_daily_reset_next_day_halt_auto_rollback_to_running():
        ctl, cfg, ctl_store, rt_store, now_fake, true_sa, expected_upt = _make_runtime(status_from_ctl_expected_match=True)
        # Day 0：status=HALT，daily_reset_day = 2026-08-31（真 yesterday）
        ctl_store.save({**ctl_store.load(), "status": SystemStatus.HALT.value,
                        "daily_reset_day": "2026-08-31"})
        assert ctl_store.load().get("status") == SystemStatus.HALT.value
        # 模拟今天 = 次日：2026-09-01（≠ 2026-08-31）
        changed = ctl.apply_daily_reset_if_needed(today="2026-09-01")
        assert changed is True
        st = ctl_store.load()
        assert st.get("status") == SystemStatus.RUNNING.value, (
            f"新交易日 HALT 未自动恢复 RUNNING，status={st.get('status')}"
        )
        assert st.get("daily_reset_day") == "2026-09-01"
        # 60s 冷却时间戳应被清除
        assert "_liq_close_last_fail_ts" not in st

    @staticmethod
    def test_apply_daily_reset_same_day_halt_stays_halt():
        """当天还没过完 → HALT 不允许自动恢复，强平保护的当日语义保持。"""
        ctl, cfg, ctl_store, rt_store, now_fake, true_sa, expected_upt = _make_runtime(status_from_ctl_expected_match=True)
        ctl_store.save({**ctl_store.load(), "status": SystemStatus.HALT.value,
                        "daily_reset_day": "2026-09-01"})
        changed = ctl.apply_daily_reset_if_needed(today="2026-09-01")
        st = ctl_store.load()
        # changed=True 也没问题（daily_reset_day 首次写入也算"日切发生"）；关键 status 不能被偷偷切回 RUNNING
        assert st.get("status") == SystemStatus.HALT.value, (
            f"同日 HALT 不应自动消掉：status={st.get('status')}"
        )
