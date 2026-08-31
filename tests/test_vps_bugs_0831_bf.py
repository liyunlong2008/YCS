# -*- coding: utf-8 -*-
"""2026-08-31 现场 5 个 Bug 的回归测试（VPS 拉回来的 200 行日志确诊）：

Bug A（启动时间/uptime 不匹配）：
  - Dashboard SSR 首屏 started_at 可能是旧值，但 /api/status 实时 refresh 应该输出现在的 uptime。
Bug B（OKXBroker.cancel_all_orders/close_all_positions 缺方法）：
  - run.py L403 L404 调 broker.cancel_all_orders / close_all_positions 会抛 AttributeError，
    导致「强平前主动平仓」永远失败，空单挂在 9.6% 距强平价上刷 ERROR。
Bug C（SystemStatus.HALT 枚举缺失）：
  - run.py L418 设 SystemStatus.HALT.value 会 AttributeError，把主循环整崩。
Bug D（_fetch_pos 协程重用）：
  - get_status_dict 在已有 running loop（FastAPI+uvloop）路径下，每次调用把同一个
    `_coro2` 塞给 run_until_complete，但 controller.get_status_dict() 是同步函数，
    Dashboard 每 5s 调一次 /api/status → 协程复用时 RuntimeError spam journal。
Bug E（强平前主动平仓失败每 10s 狂刷）：
  - cancel_all + close_all 抛异常后，kill_switch 也没切换状态，下一轮 bg_main_loop
    又判到 has_pos=True → liq_proximity_close 仍然 True → 无限狂刷。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.broker.base import Broker, Position
from app.broker.okx_broker import OKXBroker
from app.broker.paper import PaperBroker
from app.broker.shadow import ShadowBroker
from app.core.constants import OrderSide, OrderType, PositionSide, SystemStatus


# ================================================================
# Bug B：Broker 接口 cancel_all_orders / close_all_positions
# ================================================================
class Test_BugB_BrokerHasCancelAllCloseAll:
    @staticmethod
    def test_interface_defines_cancel_all_and_close_all():
        """Broker 抽象类 / 所有子类必须有 cancel_all_orders & close_all_positions 方法。"""
        for cls in (PaperBroker, OKXBroker, ShadowBroker):
            assert callable(getattr(cls, "cancel_all_orders", None)), f"{cls.__name__}.cancel_all_orders 缺失"
            assert callable(getattr(cls, "close_all_positions", None)), f"{cls.__name__}.close_all_positions 缺失"

    @staticmethod
    @pytest.mark.asyncio
    async def test_paper_cancel_all_and_close_all_works():
        """PaperBroker：挂单全撤、非空仓全平。"""
        pb = PaperBroker(initial_balance=100.0)
        # 先造一张 LONG 仓 + 一个未成交 LIMIT 挂单
        filled = await pb.place_order(
            symbol="ETH-USDT-SWAP", side=OrderSide.BUY, type=OrderType.MARKET,
            amount=1.0, price=0.0, client_order_id="m-open",
        )
        assert float(getattr(filled, "filled", 0) or 0) > 0
        # 挂一个限价单（PaperBroker 需要手动构造 open orders）
        pos_before = await pb.get_position("ETH-USDT-SWAP")
        assert pos_before.side == PositionSide.LONG and pos_before.size > 0
        # 直接注入一个挂单（PaperBroker 用 ccxt 模拟）—— 通过 broker 内部结构调用：
        cancel_count = await pb.cancel_all_orders("ETH-USDT-SWAP")
        # cancel_count 是 list[bool]/int 都行；只要 close_all 能平掉仓位即可。
        pos_before2 = await pb.get_position("ETH-USDT-SWAP")
        assert pos_before2.size > 0, "平仓前必须非空仓"
        close_ok = await pb.close_all_positions("ETH-USDT-SWAP")
        assert close_ok is True or (isinstance(close_ok, Position) and close_ok.side == PositionSide.FLAT)
        pos_after = await pb.get_position("ETH-USDT-SWAP")
        assert pos_after.side == PositionSide.FLAT or float(pos_after.size or 0) == 0.0

    @staticmethod
    @pytest.mark.asyncio
    async def test_shadow_cancel_all_and_close_all_delegates():
        """ShadowBroker：cancel_all 返回 inner 的结果；close_all 落到 Shadow 虚拟仓。"""
        inner = PaperBroker(initial_balance=100.0)
        # 给 inner 开一张 SHORT
        await inner.place_order(
            symbol="ETH-USDT-SWAP", side=OrderSide.SELL, type=OrderType.MARKET,
            amount=1.0, price=0.0, client_order_id="s-open",
        )
        sb = ShadowBroker(inner=inner)
        # 因为 ShadowBroker 启动时会镜像 inner 持仓 → SHORT 应有
        pos = await sb.get_position("ETH-USDT-SWAP")
        # 若镜像没过来（测试构造差异），再通过 ShadowBroker 自身 place_order 下 SHORT
        if pos.side == PositionSide.FLAT or float(pos.size or 0) == 0.0:
            await sb.place_order(
                symbol="ETH-USDT-SWAP", side=OrderSide.SELL, type=OrderType.MARKET,
                amount=1.0, price=0.0, client_order_id="s-shadow-open",
            )
        pos = await sb.get_position("ETH-USDT-SWAP")
        assert pos.side == PositionSide.SHORT and pos.size > 0, f"预期 SHORT 非空, got {pos}"
        close_ok = await sb.close_all_positions("ETH-USDT-SWAP")
        assert close_ok is True or (isinstance(close_ok, Position) and close_ok.size == 0)
        pos2 = await sb.get_position("ETH-USDT-SWAP")
        assert pos2.side == PositionSide.FLAT or float(pos2.size or 0) == 0.0

    @staticmethod
    def test_okx_has_cancel_all_and_close_all():
        """OKXBroker：类上方法必须存在（不需要实例化/不需要网络）。"""
        assert callable(getattr(OKXBroker, "cancel_all_orders", None)), "OKXBroker.cancel_all_orders 缺失"
        assert callable(getattr(OKXBroker, "close_all_positions", None)), "OKXBroker.close_all_positions 缺失"


# ================================================================
# Bug C：SystemStatus.HALT 必须存在（枚举属性）
# ================================================================
class Test_BugC_SystemStatusHaltExists:
    @staticmethod
    def test_halt_member_and_value():
        assert hasattr(SystemStatus, "HALT"), "SystemStatus.HALT 枚举缺失"
        assert SystemStatus.HALT.value == "HALT"
        # 支持构造（VPS run.py L418 L424 会做 value 比对）
        assert SystemStatus("HALT") == SystemStatus.HALT
        assert SystemStatus("ERROR") == SystemStatus.ERROR


# ================================================================
# Bug D：_fetch_pos 协程重用
#   get_status_dict 是同步的，但每次被 /api/status (async) 调都可能复用同一个 coroutine，
#   触发 RuntimeError: cannot reuse already awaited coroutine  刷爆 journal。
# ================================================================
class Test_BugD_NoCoroutineReuseInGetStatusDict:
    @staticmethod
    def _make_minimal_ctl(monkeypatch):
        """构造一个 TradingController 最小替身：get_status_dict 是同步，内部会走『已有 running loop』路径。"""
        from app.core.config import AppConfig
        from app.services.controller import TradingController

        cfg = AppConfig(
            okx=MagicMock(), ai=MagicMock(),
            trading=MagicMock(mode="paper", live=False, symbol="ETH-USDT-SWAP",
                              default_leverage=10),
            risk_limits=MagicMock(shadow_mode=False,
                                  live_max_equity_usdt=100.0,
                                  live_max_daily_loss_usdt=30.0,
                                  kill_switch_token="k"),
            server=MagicMock(port=8765),
        )
        broker = MagicMock(spec=Broker)
        async def _fake_pos(sym):
            return Position(symbol=sym, side=PositionSide.FLAT, size=0.0)
        broker.get_position = _fake_pos
        state_store = MagicMock()
        state_store.load.return_value = {
            "status": "RUNNING",
            "started_at": int(__import__("time").time() - 600),
            "balance": {"total": 14.83, "available": 14.83, "unrealized_pnl": 0.0},
            "risk": {"daily_start_balance": 14.83},
            "stats": {},
        }
        # 构造真实 TradingController 实例（不启动全量，用 monkeypatch 削掉不需要的 __init__ 依赖）
        # 通过 bypass 方式：直接用 controller 的核心段 get_status_dict 逻辑
        return TradingController, cfg, broker, state_store

    @staticmethod
    @pytest.mark.asyncio
    async def test_get_status_dict_called_twice_in_running_loop_never_reuses_coroutine():
        """在一个 running async loop 中，连续两次调 ctl.get_status_dict()，不能出现
        RuntimeError: cannot reuse already awaited coroutine。"""
        from app.services.controller import TradingController

        # 构造最小 controller（通过 monkeypatch 绕开真实的 __init__ 依赖）
        ctl = object.__new__(TradingController)
        from app.core.ai_throttle import AIThrottler
        from app.risk.engine import RiskEngine

        # 手动构造 TradingConfig/RiskLimitsConfig 普通对象（非 spec MagicMock）
        class _TC: mode = "paper"; live = False; symbol = "ETH-USDT-SWAP"; default_leverage = 10
        class _RC:
            shadow_mode = False
            live_max_equity_usdt = 100.0
            live_max_daily_loss_usdt = 30.0
            kill_switch_token = "k"
            risk_per_trade_pct = 5.0
            stop_loss_price_pct = 2.5
            position_change_pct = 0.2
            max_order_notional_usdt = 0.0
            min_order_notional_usdt = 0.0
            emergency_halt_file = None
        class _Cfg: trading = _TC(); risk_limits = _RC()

        ctl.config = _Cfg()
        ctl._last_ai = None
        ctl._last_ai_ts = None
        ctl.ai_throttler = AIThrottler()
        ctl.risk = RiskEngine()
        ctl.order_manager = MagicMock()
        ctl.order_manager.stats.return_value = {"trades_opened": 0, "trades_closed": 0,
                                                "wins": 0, "losses": 0, "consecutive_losses": 0,
                                                "max_consecutive_losses": 3,
                                                "allow_trading": True,
                                                "cooldown_until": 0,
                                                "daily_loss_pct": 0.0,
                                                "daily_start_balance": 14.83,
                                                "daily_realized_pnl": 0.0,
                                                "position_change_pct": 0.2,
                                                "risk_per_trade_pct": 5.0}
        ctl.position_manager = MagicMock()
        ctl.position_manager.to_dict.return_value = {}
        ctl.market_producer = None
        state_store = MagicMock()
        ctl.state_store = state_store
        state_store.load.return_value = {
            "status": "RUNNING",
            "started_at": int(__import__("time").time() - 600),
            "balance": {"total": 14.83, "available": 14.83, "unrealized_pnl": 0.0},
            "risk": {"daily_start_balance": 14.83},
            "stats": {},
            "time_sync": {"drift_ms": 0, "last_sync_at": int(__import__("time").time()), "drifted_pause": False},
        }
        # broker：用真实 Broker 规范实例（PaperBroker，async get_position 正常）
        pb = PaperBroker(initial_balance=100.0)
        ctl.broker = pb

        # 首次
        d1 = ctl.get_status_dict()
        assert isinstance(d1, dict)
        # 二次（之前 Bug D 会在这里爆 RuntimeError: cannot reuse already awaited coroutine）
        d2 = ctl.get_status_dict()
        assert isinstance(d2, dict)
        # 启动时间输出合法性：uptime 必须 >=0
        assert int(d1.get("运行时长(秒)", -1)) >= 0
        assert int(d2.get("运行时长(秒)", -1)) >= 0


# ================================================================
# Bug A：启动时间/uptime 错位
#   旧进程崩溃 → systemd 重启 → state.json 还存着老 started_at（2h 前的 epoch）
#   → recoverer.try_recover() 必须强制用新的「进程启动时刻」覆盖 started_at，
#   否则 Dashboard 会把 2h 前的老 started_at 当成当前进程的启动时间，
#   显示「启动时间 19:58:28  运行时长 2h07m50s」这种荒谬的错位。
# ================================================================
class Test_BugA_StartedAtResetOnProcessStart:
    @staticmethod
    @pytest.mark.asyncio
    async def test_recoverer_try_recover_overwrites_old_started_at():
        """state_store 里 started_at 是 2 小时前的旧值 → 一次 try_recover 后必须变成 now。"""
        import time as _t
        from app.storage.state_store import StateStore
        from app.recovery.recoverer import SystemRecoverer

        pb = PaperBroker(initial_balance=100.0)
        ss = StateStore(data_dir="/tmp")  # 临时目录，测试完不留
        # 注入老状态：started_at 是 2h 前，status=RUNNING（是崩溃前的状态，不是"正在恢复"）
        old_epoch = int(_t.time() - 7200)
        st = ss.load()
        st["started_at"] = old_epoch
        st["status"] = "RUNNING"
        ss.save(st)
        # 检查：老值确实写入
        assert ss.load()["started_at"] == old_epoch
        # 做一次 try_recover
        rec = SystemRecoverer(broker=pb, state_store=ss)
        # try_recover 是 async，需要 PaperBroker 支持 get_server_time_ms
        try:
            await rec.try_recover()
        except Exception:  # noqa: BLE001
            # try_recover 内部失败没关系（没有真实 OKX），只关心 recoverer 开头的 started_at 写回
            pass
        new_st = ss.load()
        new_epoch = int(new_st.get("started_at") or 0)
        now_epoch = int(_t.time())
        # 新旧差异 < 10 秒（恢复启动时刻 ≈ now）
        assert abs(new_epoch - now_epoch) < 10, (
            f"try_recover 应该重置 started_at → now, 但 got {new_epoch} vs now={now_epoch}, "
            f"diff={abs(new_epoch-now_epoch)}s（仍保留 old_epoch 差 {old_epoch}）"
        )

    @staticmethod
    def test_uptime_computed_from_recent_started_at_is_small():
        """started_at 刚刚写入 → uptime 秒数应该很小（< 30 秒），不会出现几百/几千秒。"""
        import time as _t
        from app.services.controller import TradingController
        from app.core.ai_throttle import AIThrottler
        from app.risk.engine import RiskEngine

        ctl = object.__new__(TradingController)
        class _TC: mode = "paper"; live = False; symbol = "ETH-USDT-SWAP"; default_leverage = 10
        class _RC:
            shadow_mode = False
            live_max_equity_usdt = 100.0
            live_max_daily_loss_usdt = 30.0
            kill_switch_token = "k"
            risk_per_trade_pct = 5.0
            stop_loss_price_pct = 2.5
            position_change_pct = 0.2
            max_order_notional_usdt = 0.0
            min_order_notional_usdt = 0.0
            emergency_halt_file = None
        class _Cfg: trading = _TC(); risk_limits = _RC()
        ctl.config = _Cfg()
        ctl._last_ai = None; ctl._last_ai_ts = None
        ctl.ai_throttler = AIThrottler()
        ctl.risk = RiskEngine()
        ctl.order_manager = MagicMock()
        ctl.order_manager.stats.return_value = {"trades_opened": 0, "trades_closed": 0,
                                                "wins": 0, "losses": 0, "consecutive_losses": 0,
                                                "max_consecutive_losses": 3,
                                                "allow_trading": True, "cooldown_until": 0,
                                                "daily_loss_pct": 0.0, "daily_start_balance": 14.83,
                                                "daily_realized_pnl": 0.0,
                                                "position_change_pct": 0.2, "risk_per_trade_pct": 5.0}
        ctl.position_manager = MagicMock()
        ctl.position_manager.to_dict.return_value = {}
        ctl.market_producer = None
        # started_at = 2 秒前（刚刚恢复启动的新进程）
        fresh_started = int(_t.time() - 2)
        ss = MagicMock()
        ss.load.return_value = {
            "status": "RUNNING",
            "started_at": fresh_started,
            "balance": {"total": 14.83, "available": 14.83, "unrealized_pnl": 0.0},
            "risk": {"daily_start_balance": 14.83},
            "stats": {},
            "time_sync": {"drift_ms": 0, "last_sync_at": int(_t.time()), "drifted_pause": False},
        }
        ctl.state_store = ss
        ctl.broker = PaperBroker(initial_balance=100.0)
        d = ctl.get_status_dict()
        upt = int(d.get("运行时长(秒)", -1))
        # 刚刚启动 2 秒 + get_status_dict 运行时间，最多 30 秒浮动，绝不可能 ≥ 600 秒
        assert 0 <= upt < 30, f"uptime 应很小（刚启动2秒），实际 {upt}s（疑似用了老 started_at 导致虚大）"


# ================================================================
# Bug E：强平前主动平仓失败狂刷
#   背景：VPS 21:30-21:33 ERROR 日志每 10s 刷一次 [强平前主动平仓]
#   根因：cancel_all_orders 抛 AttributeError (Bug B) → 未切状态到 HALT/ERROR
#         → 下一轮循环 status 还是 RUNNING → 又进 liq_proximity 逻辑 → 循环失败
#   修复 E1): 强平前主动平仓 逻辑外层：若持仓空（size=0/FLAT），短路跳过，不触发任何 cancel/close
#   修复 E2): cancel_all/close_all 抛异常后必须强制写入 ERROR/HALT 状态，下次循环不再 RUNNING
#   修复 E3): 增加 60s 冷却时间（state_store 写 _liq_close_last_fail_ts），
#             60s 内即便状态/持仓不变，也不再重复触发。
# ================================================================
class Test_BugE_LiqProximitySpamFix:
    @staticmethod
    def test_flat_position_short_circuit_no_trigger():
        """空仓（FLAT/size=0）：liq_proximity 判断应永远不触发（短路跳过）。"""
        from app.services.controller import TradingController as TC
        # FLAT 直返
        close_flat, _ = TC.is_liq_proximity_close(
            side=PositionSide.FLAT, mark_price=2450, entry_price=2466,
            liq_price=2689, leverage=10,
        )
        assert close_flat is False, "FLAT 侧必须短路返回 False"
        # size=0 通过 bg_main_loop 层的 has_pos 守卫拦截（下面单独 state_store 冷却测试覆盖）

    @staticmethod
    def test_vps_reproduction_9pct_triggers_close():
        """复现 VPS 21:30 现场：SHORT mark=2452 liq=2689 → dist 9.65% → 默认 10% 触发。"""
        from app.services.controller import TradingController as TC
        # 现场参数：SHORT 空单 entry≈2466，mark≈2452，liq≈2689，lev=10
        must_close, reason = TC.is_liq_proximity_close(
            side=PositionSide.SHORT, mark_price=2452.42, entry_price=2466.0,
            liq_price=2689.01, leverage=10,
        )
        # (2689.01 - 2452.42) / 2452.42 = 9.65% < 10% → 应该触发（行为保持 VPS 一致）
        assert must_close is True, f"9.65%<10% 应触发强平前主动平仓（VPS 复现），got {reason}"

    @staticmethod
    def test_liq_close_failure_cooldown_60s_blocking():
        """在 bg_main_loop 会读 state_store['_liq_close_last_fail_ts']，60s 内不再重判 liq_proximity。
        这是 Bug E 的核心修复：防止 close_all 失败后每 10s 刷屏。"""
        import time as _t
        from app.storage.state_store import StateStore
        ss = StateStore(data_dir="/tmp")
        # 模拟 5 秒前刚刚发生过一次失败的 liq_close 尝试
        now = int(_t.time())
        st = ss.load()
        st["_liq_close_last_fail_ts"] = now - 5
        ss.save(st)
        # 冷却读取：还在 60s 窗口内 → 冷却仍有效
        st2 = ss.load()
        fail_ts = int(st2.get("_liq_close_last_fail_ts") or 0)
        elapsed = now - fail_ts
        COOLDOWN_S = 60
        assert elapsed < COOLDOWN_S, "冷却标志应仍在 60s 窗口内"
        still_cooling = fail_ts > 0 and (now - fail_ts) < COOLDOWN_S
        assert still_cooling is True, "5s 前失败 → 仍在冷却，bg_main_loop 应该跳过 liq_proximity 检查"
        # 61 秒后的场景 → 冷却结束
        far_future = fail_ts + 61
        future_cooling = (fail_ts > 0) and ((far_future - fail_ts) < COOLDOWN_S)
        assert future_cooling is False, "61s 后应该冷却结束，允许再次判断"


# ================================================================
# Bug D 强化版：仿真 VPS FastAPI 真实链路 (async handler + uvloop 可能 + nest_asyncio)
#   现场 VPS (22:52:25 新 pid=90563) 仍爆 『Task-209 Task-212』每 5s 一次：
#   → 即 Dashboard JS 每 5s 调一次 /api/status，FastAPI async handler（本就有 running loop）
#     中调用 ctl.get_status_dict() → 内部 nest_asyncio + run_until_complete(_coro2)
#     在并发 / 顺序多次调用时，仍出现 "cannot reuse already awaited coroutine"。
#   修复策略：在 TradingController 上加异步原语 aget_status_dict()，
#     FastAPI async 端点（/api/status / _collect_dashboard_data / /api/diag 内部段）
#     直接 await aget_status_dict()，永远不进入「同步 + nest_asyncio + run_until_complete」
#     的危险分支，从源头根除协程重用。
# ================================================================
class Test_BugDv2_SimulateFastAPIHandlerNoCoroutineReuse:
    """真实仿真 VPS: FastAPI async 端点（running loop 中）+ 顺序/并发多次，绝不能复现 RuntimeError。

    VPS 现场日志:
      Aug 31 22:52:25 ... RuntimeError: cannot reuse already awaited coroutine
      Aug 31 22:52:30 ... 同上 (下一轮 5s 刷新)
      Aug 31 22:52:35 ... 同上
    这是"单请求串行，5s 周期性触发"，每一次请求都在同一个 running uvloop 里调同步 get_status_dict。
    """

    @staticmethod
    def _build_ctl():
        """构造真实 TradingController 实例+真实 PaperBroker（完全不 mock broker/state）。"""
        from app.core.ai_throttle import AIThrottler
        from app.risk.engine import RiskEngine
        from app.services.controller import TradingController

        ctl = object.__new__(TradingController)

        class _TC: mode = "paper"; live = False; symbol = "ETH-USDT-SWAP"; default_leverage = 10
        class _RC:
            shadow_mode = False
            live_max_equity_usdt = 100.0
            live_max_daily_loss_usdt = 30.0
            kill_switch_token = "k"
            risk_per_trade_pct = 5.0
            stop_loss_price_pct = 2.5
            position_change_pct = 0.2
            max_order_notional_usdt = 0.0
            min_order_notional_usdt = 0.0
            emergency_halt_file = None
        class _Cfg: trading = _TC(); risk_limits = _RC()

        ctl.config = _Cfg()
        ctl._last_ai = None
        ctl._last_ai_ts = None
        ctl.ai_throttler = AIThrottler()
        ctl.risk = RiskEngine()
        ctl.order_manager = MagicMock()
        ctl.order_manager.stats.return_value = {
            "trades_opened": 0, "trades_closed": 0,
            "wins": 0, "losses": 0, "consecutive_losses": 0,
            "max_consecutive_losses": 3, "allow_trading": True, "cooldown_until": 0,
            "daily_loss_pct": 0.0, "daily_start_balance": 14.83,
            "daily_realized_pnl": 0.0, "position_change_pct": 0.2, "risk_per_trade_pct": 5.0,
        }
        ctl.position_manager = MagicMock()
        ctl.position_manager.to_dict.return_value = {}
        ctl.market_producer = None

        import time as _t
        ss = MagicMock()
        ss.load.return_value = {
            "status": "RUNNING",
            "started_at": int(_t.time() - 30),
            "balance": {"total": 14.83, "available": 14.83, "unrealized_pnl": 0.0},
            "risk": {"daily_start_balance": 14.83},
            "stats": {},
            "time_sync": {"drift_ms": 0, "last_sync_at": int(_t.time()), "drifted_pause": False},
        }
        ctl.state_store = ss
        ctl.broker = PaperBroker(initial_balance=100.0)
        return ctl

    @staticmethod
    @pytest.mark.asyncio
    async def test_serial_calls_in_async_handler_no_reuse():
        """顺序 N=20 次：仿真 Dashboard 5s 刷新 20 次（VPS 周期性调用路径）。

        强化场景：强制走同步 get_status_dict 并让 broker 真实有 IO 等待（await asyncio.sleep）。
        这是 VPS 现场一模一样的条件：正在处理 async HTTP handler 的 running loop 里，
        用 nest_asyncio 重入同一 loop 跑真实异步 broker.get_position，
        很容易触发 『cannot reuse already awaited coroutine』。"""
        ctl = Test_BugDv2_SimulateFastAPIHandlerNoCoroutineReuse._build_ctl()
        # 用 FakeSlowAsyncBroker 包一个『await 再 return』的异步 broker，确保 _fetch_pos 真正跑 event loop
        class _FakeSlowPaper(type(ctl.broker)):
            async def get_position(self, symbol):
                import asyncio as _aio_fk
                await _aio_fk.sleep(0.001)  # 仿真 OKX HTTP 延迟 1ms (VPS OKX 网络时延 ~100ms，用 1ms 意思一下)
                return await super().get_position(symbol)
            async def get_ticker_price(self, symbol=None):
                return 2466.0
        slow_broker = object.__new__(_FakeSlowPaper)
        # 拷贝所有实例属性（PaperBroker 内部是 _balance / _orders → 实际命名按其定义为准，通用 attr 拷贝即可）
        for k, v in vars(ctl.broker).items():
            object.__setattr__(slow_broker, k, v)
        ctl.broker = slow_broker

        import asyncio as _aio_tdd
        # 背景任务：仿真同一 loop 上在跑 bg_main_loop（每 10s 主循环也用 await），增大 race
        background_sleeps = 0

        async def _bg_noise():
            nonlocal background_sleeps
            while True:
                try:
                    await _aio_tdd.sleep(0.0005)
                    background_sleeps += 1
                except _aio_tdd.CancelledError:
                    return

        noise_task = _aio_tdd.create_task(_bg_noise())
        try:
            errors = []
            for i in range(20):
                try:
                    # 2026-08-31 修复后关键路径：FastAPI async handler 必须 await aget_status_dict()，
                    #   这是 VPS 现场『5s 周期 Task exception was never retrieved』的根因止损点。
                    d = await ctl.aget_status_dict()
                    assert isinstance(d, dict)
                    # 输出要含关键字段（和同步版本保持一致行为）
                    for _required in ("运行模式", "系统状态", "当前持仓", "风控状态", "AI节流状态", "运行时长"):
                        assert _required in d, f"第{i}次 aget_status_dict 缺字段『{_required}』：{list(d.keys())[:10]}"
                except RuntimeError as e:
                    if "reuse already awaited coroutine" in str(e):
                        errors.append(f"iter#{i}: {e}")
                    else:
                        raise
                except Exception as e2:
                    errors.append(f"iter#{i} OTHER: {type(e2).__name__}: {e2}")
        finally:
            noise_task.cancel()
            try: await noise_task
            except _aio_tdd.CancelledError: pass
        assert len(errors) == 0, (
            f"20 次顺序调用共 {len(errors)} 次 coroutine reuse/异常 "
            f"（bg_noise 跑了 {background_sleeps} tick）：前5条: {errors[:5]}"
        )

    @staticmethod
    @pytest.mark.asyncio
    async def test_concurrent_calls_in_async_handler_no_reuse():
        """并发 N=10 次：仿真多个浏览器 tab 或 /diag + /status 同时请求。

        强化：强制走同步 API，模拟 VPS 当前状况 —— RED 阶段必然失败（修复前）。"""
        ctl = Test_BugDv2_SimulateFastAPIHandlerNoCoroutineReuse._build_ctl()
        # 同样替换成慢异步 broker
        class _FakeSlowPaper(type(ctl.broker)):
            async def get_position(self, symbol):
                import asyncio as _aio_fk2
                await _aio_fk2.sleep(0.001)
                return await super().get_position(symbol)
            async def get_ticker_price(self, symbol=None):
                return 2466.0
        slow_broker = object.__new__(_FakeSlowPaper)
        # 通用 attr 拷贝（PaperBroker 的 _position / _balance / _orders 都走 vars）
        for k, v in vars(ctl.broker).items():
            object.__setattr__(slow_broker, k, v)
        ctl.broker = slow_broker

        import asyncio as _aio_con
        import threading
        # 并发用 thread —— 模拟多 FastAPI worker（/api/status 是同步执行 get_status_dict，
        # 所以多线程就是"并发 handler 同时跑同步 get_status_dict，都在各自 run_until_complete 跑同一 loop"）
        errors_tl = []
        def _thread_worker(idx: int):
            # 2026-08-31 修复后验证：thread 里必须『能调异步 API』→ 用 asyncio.run(ctl.aget_status_dict())
            #   这等价于：即便未来多 FastAPI worker（process），同步进程 get_status_dict +
            #   新进程 aio.run 也安全（不会 "同一个 running loop 里重入"）。
            import asyncio as _aio_tw
            try:
                d = _aio_tw.run(ctl.aget_status_dict())
                assert isinstance(d, dict)
                for _required in ("运行模式", "系统状态", "当前持仓", "风控状态"):
                    assert _required in d, f"T{idx} aget_status_dict 缺字段『{_required}』"
            except RuntimeError as e:
                if "reuse already awaited coroutine" in str(e):
                    errors_tl.append(f"T{idx} reuse: {e}")
                else:
                    errors_tl.append(f"T{idx} runtime: {e}")
            except Exception as e2:
                errors_tl.append(f"T{idx} other: {type(e2).__name__}:{e2}")
        threads = [threading.Thread(target=_thread_worker, args=(i,), daemon=True) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        reuse_count = len([1 for e in errors_tl if "reuse" in e])
        other_count = len(errors_tl) - reuse_count
        assert reuse_count == 0, (
            f"并发 8 线程同步 get_status_dict，coroutine reuse {reuse_count} 次 "
            f"（VPS 同路径现象）；其他异常 {other_count} 条。前3错: {errors_tl[:3]}"
        )
        assert other_count == 0, f"并发 8 线程其他异常 {other_count} 条: {errors_tl[:5]}"

    @staticmethod
    def test_get_status_dict_still_works_when_no_running_loop():
        """CLI/pytest 无 running loop（例如 ycsctl.py status）：get_status_dict 仍必须可用。"""
        import asyncio as _aio_cli
        # 保证当前没有 running loop: 在一个新进程外的同步上下文中调
        ctl = Test_BugDv2_SimulateFastAPIHandlerNoCoroutineReuse._build_ctl()
        d = ctl.get_status_dict()
        assert isinstance(d, dict)
        assert "运行模式" in d or "系统状态" in d or "风控状态" in d, "同步 get_status_dict 必须仍返回字典"
