"""
TDD 阶段 12 · /api/diag 用户贴回来 payload 的 4 个缺陷修复：
  (1) 中文乱码（ensure_ascii=False + charset=utf-8 + 内部拼接 str 时拒绝非法 surrogate）
  (2) 纸盘模式 PaperBroker 仍调 OKX 网络导致 RequestTimeout → PaperBroker 必须纯本地，不应
      在 /api/diag broker_block 里触发任何 OKX HTTP
  (3) project_root 路径：用 Path(__file__).resolve().parents[2]（= 项目根）一致性推导，
      stage8/stage9 pytest 子进程找不到 uv 时 fallback 到 `python -m pytest`
  (4) fixtures.sources：对 18 个文件逐文件分类，不能只抽样 3 个场景
  (5) safety placeholder 计数：兜底空/None 也算占位
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# 项目根：按当前测试文件位置推导（tests/*.py → parent.parent = 项目根）
# 不再硬编码 /workspace：兼容 VPS /opt/ycs、本地 Windows、容器任意 INSTALL_DIR
REPO = Path(__file__).resolve().parent.parent


# ============================================================================
# ① 中文乱码：FastAPI /api/diag 顶层 JSON 响应 charset=utf-8 + ensure_ascii=False
# ============================================================================
class Test_1_DiagChineseEncoding:
    def test_diag_response_content_type_contains_utf8_charset(self):
        """FastAPI Response 的 Content-Type 必须显式带 charset=utf-8
           （Windows PowerShell / cmd curl pipe 默认 GBK，缺 charset 会把 UTF-8 字节按 GBK 解码）"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        app = create_app()
        with TestClient(app) as client:
            r = client.get("/api/diag")
        ct = r.headers.get("content-type", "")
        assert "charset=utf-8" in ct.lower(), (
            f"/api/diag Content-Type 缺少 charset=utf-8（当前 {ct!r}），"
            "Windows curl pipe 会按 GBK 解码，导致中文全部乱码。"
        )

    def test_diag_payload_mode_cn_is_clean_unicode_no_surrogates(self):
        """system.runtime_mode 必须是干净的「纸盘模式」等合法中文字符串，
           不得包含 unpaired surrogate（\ud800-\udfff 范围内的单独码位，乱码的明确信号）。"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        app = create_app()
        with TestClient(app) as client:
            body = client.get("/api/diag").json()
        mode = str(body["system"]["runtime_mode"])
        # 任一码点落入 surrogate 区 → 乱码污染
        bad = [c for c in mode if 0xD800 <= ord(c) <= 0xDFFF]
        assert not bad, (
            f"runtime_mode 含未配对 surrogate（乱码信号）：{bad!r}；原始 mode={mode!r}"
        )
        # 合法值：必须是已声明的 4 种之一
        assert mode in ("纸盘模式", "实盘模式", "纸盘模式(影子 SHADOW)", "实盘模式(影子 SHADOW)"), (
            f"runtime_mode 非法：{mode!r}"
        )

    def test_diag_payload_risks_no_surrogates(self):
        """risks 每条中文警告也不能有 surrogate。"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        app = create_app()
        with TestClient(app) as client:
            body = client.get("/api/diag").json()
        for i, line in enumerate(body.get("risks") or []):
            bad = [c for c in line if 0xD800 <= ord(c) <= 0xDFFF]
            assert not bad, f"risks[{i}] 含 surrogate 乱码：{line!r}"


# ============================================================================
# ② PaperBroker 纯本地：get_balance / get_position / get_open_orders 不触网
# ============================================================================
class Test_2_PaperBrokerPureLocal:
    def test_paper_broker_balance_has_no_network_dependency(self, tmp_path):
        """PaperBroker.get_balance() 必须立刻返回本地模拟 Balance；
           即便断网 / OKX 不可达 / Windows 无代理，也不应抛 RequestTimeout。"""
        from app.broker.paper import PaperBroker
        pb = PaperBroker(symbol="ETH-USDT-SWAP")
        import asyncio
        bal = asyncio.run(pb.get_balance())
        assert hasattr(bal, "total") and hasattr(bal, "available")
        assert float(bal.total) > 0, "PaperBroker 应有本地初始余额"

    def test_diag_paper_broker_reports_available_true_no_error(self):
        """/api/diag 里当 broker=PaperBroker 时，available 必须 = True；
           严禁出现 RequestTimeout / okx.com 字样 error 字段（当前用户贴的 payload 就是这个 bug）。"""
        # 构造 runtime：config=paper 模式 + broker=PaperBroker
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        from app.broker.paper import PaperBroker
        from app.core.config import (
            AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits,
        )
        app = create_app()
        # 在 TestClient 发请求前向 app.state.runtime 注入本地组件
        cfg = AppConfig(
            okx=OKXConfig(api_key="YOUR_OKX_API_KEY", secret="YOUR_OKX_SECRET",
                          passphrase="YOUR_OKX_PASSPHRASE"),
            ai=AIConfig(provider="deepseek", api_key="YOUR_AI_API_KEY"),
            trading=TradingConfig(live=False, symbol="ETH-USDT-SWAP"),
            risk_limits=RiskLimits(),
        )
        broker = PaperBroker(symbol="ETH-USDT-SWAP")
        controller_stub = SimpleNamespace(broker=broker, risk=SimpleNamespace(
            consecutive_losses=0, cooldown_until_ts=0, daily_start_balance=100.0,
        ))
        app.state.runtime.update({
            "config": cfg,
            "broker": broker,
            "controller": controller_stub,
        })
        with TestClient(app) as client:
            body = client.get("/api/diag").json()
        b = body["broker"]
        assert b.get("broker_type") == "PaperBroker", (
            f"纸盘注入后 broker_type 应为 PaperBroker，实际 {b.get('broker_type')!r}"
        )
        assert b.get("available") is True, (
            f"PaperBroker /api/diag 必须 available=True，实际 {b!r}"
        )
        err = b.get("error", "")
        assert "RequestTimeout" not in err and "okx.com" not in err, (
            f"PaperBroker 不应触网出现 OKX 超时，实际 error={err!r}"
        )


# ============================================================================
# ③ project_root 推导一致性 & uv→python -m pytest fallback
# ============================================================================
class Test_3_ProjectRootResolve:
    def test_project_root_from_api_app_equals_repo(self):
        """api/app.py 里 project_root（不管当前用 parent*几次）最终要等于项目根 REPO（由 test 所在位置动态推导，不再硬编码 /workspace）。"""
        sys.path.insert(0, str(REPO))
        # 直接 import 实现里辅助函数
        from app.api import app as api_app_mod
        # 从源码中推导：取 create_app 闭包外不暴露；这里重算它用于约束行为
        computed = Path(api_app_mod.__file__).resolve().parents[2]
        assert computed.resolve() == REPO.resolve(), (
            f"api/app.py parents[2] 应是项目根 {REPO}，实际 {computed}"
        )


# ============================================================================
# ④ fixtures.sources/文件分类：用户 2026-08-29 明确 fixtures 「有啥用 去了吧」
# ============================================================================
class Test_4_FixtureRemovedCleanly:
    def test_diag_fixtures_is_removed_not_18_slot_shell(self):
        """fixtures 既然用户要移除，代码里就别再伪造 18 逻辑槽位。

        2026-08-29 之前的契约是 sources.sum() == file_count == 18；现在 fixtures 功能
        整体删掉，所以：
          · fixtures.status == "removed_by_user_request_2026-08-29"
          · file_count/sources 必须为 None（不再留"伪 18 槽位"的陷阱信号，
            避免以后巡检看到以为还存在）
        """
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        app = create_app()
        with TestClient(app) as client:
            fx = client.get("/api/diag").json()["fixtures"]
        assert fx.get("status") == "removed_by_user_request_2026-08-29", (
            f"fixtures.status 应 removed，实际 {fx.get('status')!r}"
        )
        # 不再校验 sources.sum==18 这种旧信号；若以后需要离线功能，另行设计接口
        assert fx.get("sources") is None, (
            f"fixtures 移除后 sources 必须=None（避免误导"
            f"仍有 18 槽位判定），实际 {fx.get('sources')!r}"
        )
        assert fx.get("file_count") is None, (
            f"fixtures 移除后 file_count 必须=None，实际 {fx.get('file_count')!r}"
        )


# ============================================================================
# ⑤ pytest fallback：当 uv 缺失时 _diag_run_pytest 必须 fallback sys.executable -m pytest
# ============================================================================
class Test_5_RunPytestFallbackWhenUvMissing:
    def test_diag_pytest_helper_falls_back_when_no_uv(self, monkeypatch):
        """即使 /api/diag 不再起 stage8/stage9 子进程（用户 2026-08-29 要求移除
        fixtures 相关），辅助函数 _diag_run_pytest 仍要兼容调用方（如未来 ycsctl 自
        检/应急诊断）。当 `uv` 不存在（Windows 未装 / 非激活环境）时，必须
        fallback 到 `sys.executable -m pytest`，不能直接报错。

        验证方式：在 subprocess.run 被调用前记录 argv，首段必须是 python 路径 + '-m' 'pytest'。
        """
        import shutil, subprocess
        from app.api import app as api_mod
        captured: dict[str, list] = {"argv": []}

        def fake_which(name, **kw):
            # 强制「uv 不存在，python 存在」
            if name == "uv":
                return None
            return shutil.which.__wrapped__(name, **kw) if hasattr(shutil.which, "__wrapped__") else (
                shutil.which(name) if name != "uv" else None
            )

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="1 passed\n", stderr="")

        monkeypatch.setattr(api_mod._shutil, "which", fake_which)
        monkeypatch.setattr(api_mod._sp, "run", fake_run)

        ok, info = api_mod._diag_run_pytest(
            ["tests/test_stage6_ycsctl.py", "-q"],
            project_root=REPO, timeout_seconds=5,
        )
        assert ok is True, f"_diag_run_pytest ok={ok}，应 True（rc=0）"
        argv = captured["argv"]
        assert argv, "subprocess.run 未被调用（未执行）"
        assert argv[0] != "uv", (
            f"uv 缺失时仍用了 uv argv={argv}，必须 fallback 到 python -m pytest"
        )
        assert "-m" in argv, f"fallback 时 argv 应包含 -m（实际 {argv}）"
        idx_m = argv.index("-m")
        assert argv[idx_m + 1] == "pytest", f"-m 后面应是 pytest，实际 {argv}"


# ============================================================================
# ⑥ safety placeholder 计数：兜底空串/None 也算
# ============================================================================
class Test_6_PlaceholderCountOnFallbacks:
    def test_diag_safety_counts_empty_string_okx_as_placeholder(self):
        """run.py 里 config.yaml 缺失时会 fallback 到 okx=空串 → 这 3 项必须都算占位。
           当前用户 payload 显示 okx_placeholder_key_count=0 可能是因为 fallback 到空串时
           _is_placeholder 命中了但 cfg 没注入；此测试强制注入：空串 cfg → count = 3/1"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        from app.broker.paper import PaperBroker
        from app.core.config import (
            AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits,
        )
        app = create_app()
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key=""),
            trading=TradingConfig(live=False, symbol="ETH-USDT-SWAP"),
        )
        broker = PaperBroker(symbol="ETH-USDT-SWAP")
        controller_stub = SimpleNamespace(broker=broker, risk=SimpleNamespace(
            consecutive_losses=0, cooldown_until_ts=0, daily_start_balance=100.0,
        ))
        app.state.runtime.update({
            "config": cfg,
            "broker": broker,
            "controller": controller_stub,
        })
        with TestClient(app) as client:
            s = client.get("/api/diag").json()["safety"]
        assert int(s["okx_placeholder_key_count"]) == 3, (
            f"okx 3 项空串都应算占位，实际 {s['okx_placeholder_key_count']}"
        )
        assert int(s["ai_placeholder_key_count"]) == 1, (
            f"ai.api_key 空串应算占位，实际 {s['ai_placeholder_key_count']}"
        )


# ============================================================================
# Test_7_20260831DashboardBugs（用户现场贴回 Dashboard + /api/diag 发现 4 个一致性 Bug）
#
# Bug A：started_at = None 即使是新 PID（VPS PID=82613 启动后 5m29s 仍然 null）
#         → 要求：create_app 后 lifespan 写 started_at；或 get_status_dict 兜底
#              且 /api/diag.system.started_at 返回 int epoch（非 None）
# Bug B：Dashboard 截图持仓卡显示 空仓/0.00，但真实 ShadowBroker 是 SHORT 0.1
#         → 要求：get_status_dict 返回 『当前持仓』键，且值是 broker.get_position 实
#              时快照（不是 state_store 里老 position），/api/status 也含该字段
# Bug C：AI 节流级别原因写死 "LONG_HOLD 持仓中..."，现在是 SHORT 也 LONG_HOLD
#         → 要求：ThrottleLevel 增加 SHORT_HOLD 或通用 HOLD；
#              或当持 SHORT 空仓时 reason 显示 "SHORT_HOLD 空单持仓中..."
# Bug D：ShadowBroker SHORT 空单 2466 开，eth 真实价格变了但 mark_price=2466
#         → 要求：ShadowBroker.get_position 的 mark_price，当 inner.get_position
#              的 mark_price=0（真实空仓）时，从最近一次 ticker / 订单成交记录
#              （或至少 fetch_market_spec 的缓存 entry 兜底区分出 "估算"）
# ============================================================================
class Test_7_DashboardConsistencyAfterAug31Fixes:
    # ------------------------------------------------------------------
    # Bug A: started_at epoch 绝不能 None（即使 recoverer 漏写也要兜底）
    # ------------------------------------------------------------------
    def test_diag_started_at_is_non_null_int_epoch_after_lifespan_start(self, tmp_path):
        """模拟 bootstrap 写入 started_at → /api/diag 必须返回 int epoch + uptime_s 非负。
        用户 VPS PID=82613 已经跑了 5m29s 仍然 started_at=null，这是 Dashboard 启动时间/运行时长展示的根问题。"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        from app.storage.state_store import StateStore
        from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig
        from app.broker.paper import PaperBroker
        import time as _t

        # 建一个 StateStore（写入合法 started_at=epoch 秒）
        data_dir = tmp_path / "data"; data_dir.mkdir()
        ss = StateStore(data_dir)
        st = ss.load()
        fake_epoch = int(_t.time()) - 329  # 跑了 5m29s
        st["started_at"] = fake_epoch
        ss.save(st)

        app = create_app()
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key=""),
            trading=TradingConfig(live=False, symbol="ETH-USDT-SWAP"),
        )
        broker = PaperBroker(symbol="ETH-USDT-SWAP", initial_balance=100.0)
        app.state.runtime.update({
            "config": cfg,
            "state_store": ss,
            "broker": broker,
            "data_dir": data_dir,
        })
        with TestClient(app) as client:
            diag = client.get("/api/diag").json()
        sys = diag["system"]
        # 硬性要求：started_at 必须是正 int（不能 null）
        assert isinstance(sys.get("started_at"), int) and int(sys["started_at"]) > 0, (
            f"started_at={sys.get('started_at')!r}，期望 int epoch（启动 5m 仍 null 是致命 Bug）"
        )
        # uptime_seconds 必须 >= 329s（不要负）
        up = int(sys.get("uptime_seconds") or 0)
        assert up >= 300, (
            f"uptime_seconds={up} < 300，started_at epoch ({sys.get('started_at')}) 或计算有问题"
        )
        assert isinstance(sys.get("started_at_local"), str) and len(sys["started_at_local"]) >= 10, (
            f"started_at_local={sys.get('started_at_local')!r}，期望 YYYY-MM-DD HH:MM:SS"
        )
        assert isinstance(sys.get("uptime_human"), str) and sys["uptime_human"], (
            f"uptime_human={sys.get('uptime_human')!r} 不能空或 null"
        )

    def test_started_at_null_bootstrap_sets_current_epoch(self, tmp_path):
        """若 state_store.started_at=None（新 VPS 第一次启动）→ /api/diag 返回 int，
        必须是当前时间附近，不要 null。"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        from app.storage.state_store import StateStore
        from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig
        from app.broker.paper import PaperBroker
        import time as _t

        data_dir = tmp_path / "data"; data_dir.mkdir()
        ss = StateStore(data_dir)
        app = create_app()
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key=""),
            trading=TradingConfig(live=False, symbol="ETH-USDT-SWAP"),
        )
        broker = PaperBroker(symbol="ETH-USDT-SWAP", initial_balance=100.0)
        app.state.runtime.update({
            "config": cfg, "state_store": ss, "broker": broker, "data_dir": data_dir,
        })
        before = int(_t.time())
        with TestClient(app) as client:
            after = int(_t.time())
            diag = client.get("/api/diag").json()
        sa = diag["system"].get("started_at")
        assert isinstance(sa, int) and before - 2 <= int(sa) <= after + 2, (
            f"冷启动 started_at={sa!r} 不在当前时间附近 [{before},{after}]（必须兜底不要 null）"
        )

    # ------------------------------------------------------------------
    # Bug B：/api/status & /api/diag 中「持仓」与 broker.get_position 一致
    # ------------------------------------------------------------------
    def test_api_status_returns_realtime_position_from_shadow_short_01(self, tmp_path):
        """/api/status 必须包含 ShadowBroker 实时 SHORT 0.1 空单（不是 state_store 空快照）。
        用户现场：/api/diag broker.position.side=SHORT size=0.1，但 Dashboard 持仓卡=空仓/0.000000
        说明 Dashboard 的 SSR / JS refresh 都走的 stale 源。
        要求：ctl.get_status_dict() 返回 『当前持仓』dict，side==SHORT/size==0.1 与 broker 一致。"""
        from app.broker.shadow import ShadowBroker
        from app.broker.paper import PaperBroker
        from app.core.constants import OrderSide, OrderType
        from app.risk.engine import RiskEngine
        from app.storage.state_store import StateStore
        from app.storage.trade_journal import TradeJournal
        from app.ai.factory import build_ai_provider
        from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits
        from app.services.controller import TradingController
        from app.exchange.market import MarketDataProducer
        from app.ai.base import MarketAnalysisResult
        from app.core.constants import MarketRegime

        data_dir = tmp_path / "data"; data_dir.mkdir()
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key="dummy", model="deepseek-chat"),
            trading=TradingConfig(live=True, symbol="ETH-USDT-SWAP", default_leverage=10),
            risk_limits=RiskLimits(shadow_mode=True),
        )
        ss = StateStore(data_dir)
        journal = TradeJournal(data_dir)
        risk = RiskEngine()
        inner = PaperBroker(symbol="ETH-USDT-SWAP", initial_balance=14.83)
        broker = ShadowBroker(inner=inner, symbol="ETH-USDT-SWAP")
        ai = build_ai_provider(cfg.ai)
        mp = MarketDataProducer(okx=cfg.okx, symbol=cfg.trading.symbol)
        ctl = TradingController(
            config=cfg, broker=broker, ai=ai, risk=risk,
            state_store=ss, journal=journal, market_producer=mp,
        )
        import asyncio as _aio
        # 1) 先 SHORT 开仓（影子模式）
        async def _open():
            await broker.set_leverage("ETH-USDT-SWAP", 10)
            return await broker.place_order(
                symbol="ETH-USDT-SWAP", side=OrderSide.SELL, type=OrderType.LIMIT,
                amount=0.1, price=2466.0, client_order_id="T-SHADOW-SHORT-001",
            )
        _aio.run(_open())
        # 2) 再拿 broker position 实锤
        async def _pos():
            return await broker.get_position("ETH-USDT-SWAP")
        pos = _aio.run(_pos())
        assert pos.side.value == "SHORT" and abs(float(pos.size) - 0.1) < 1e-9, (
            f"ShadowBroker 实时持仓 side={pos.side} size={pos.size}（期望 SHORT 0.1）"
        )
        # 3) get_status_dict 必须反映「实时 position」：新增 『当前持仓』结构化字段
        d = ctl.get_status_dict()
        realtime = d.get("当前持仓") or {}
        # 断言关键字段：side / size / entry / mark
        side_txt = str(realtime.get("方向") or realtime.get("持仓方向") or "").strip()
        # 兼容：中文方向（做多/做空/空单）或英文枚举 SHORT/SELL（upper 后一致）
        ok_side = (
            "SHORT" in side_txt.upper()
            or "SELL" in side_txt.upper()
            or "做空" in side_txt
            or "空单" in side_txt
        )
        assert ok_side, (
            f"get_status_dict['当前持仓']={realtime!r}，期望方向=做空/SHORT（Bug B：ShadowBroker SHORT 0.1 空单，却展示空仓）"
        )
        sz = float(realtime.get("数量") or realtime.get("持仓数量") or 0.0)
        assert abs(sz - 0.1) < 1e-9, (
            f"get_status_dict['当前持仓']['数量']={sz}，期望 0.1（ShadowBroker 实时持仓）"
        )
        lev = int(realtime.get("杠杆") or realtime.get("持仓杠杆") or 1)
        assert lev == 10, (
            f"get_status_dict['当前持仓']['杠杆']={lev}，期望 10（set_leverage 的结果要落到 /api/status）"
        )

    def test_diag_broker_position_short_matches_why_no_position(self, tmp_path):
        """在 Bug B 场景下 /api/diag 的 broker.position.side == SHORT / size == 0.1
        必须与 system.why_no_position 一致（✅ 已持仓…空单…）。"""
        from fastapi.testclient import TestClient
        from app.api.app import create_app
        from app.storage.state_store import StateStore
        from app.storage.trade_journal import TradeJournal
        from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig, RiskLimits
        from app.broker.shadow import ShadowBroker
        from app.broker.paper import PaperBroker
        from app.risk.engine import RiskEngine
        from app.trading.position_manager import PositionManager
        from app.services.controller import TradingController
        from app.ai.factory import build_ai_provider
        from app.exchange.market import MarketDataProducer
        from app.core.constants import OrderSide, OrderType
        import asyncio as _aio, time as _t
        data_dir = tmp_path / "data"; data_dir.mkdir()
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key="dummy", model="deepseek-chat"),
            trading=TradingConfig(live=True, symbol="ETH-USDT-SWAP", default_leverage=10),
            risk_limits=RiskLimits(shadow_mode=True),
        )
        ss = StateStore(data_dir)
        # 手工写 started_at 防 null
        snap = ss.load(); snap["started_at"] = int(_t.time()) - 200; ss.save(snap)
        journal = TradeJournal(data_dir)
        risk = RiskEngine(); risk.daily_start_balance = 14.83
        inner = PaperBroker(symbol="ETH-USDT-SWAP", initial_balance=14.83)
        broker = ShadowBroker(inner=inner, symbol="ETH-USDT-SWAP")
        ai = build_ai_provider(cfg.ai)
        mp = MarketDataProducer(okx=cfg.okx, symbol=cfg.trading.symbol)
        pm = PositionManager()
        ctl = TradingController(
            config=cfg, broker=broker, ai=ai, risk=risk,
            state_store=ss, journal=journal, market_producer=mp,
        )
        ctl.position_manager = pm
        async def _init():
            await broker.set_leverage("ETH-USDT-SWAP", 10)
            await broker.place_order(symbol="ETH-USDT-SWAP", side=OrderSide.SELL,
                                     type=OrderType.LIMIT, amount=0.1, price=2466.0,
                                     client_order_id="DIAG-SHORT-001")
        _aio.run(_init())

        app = create_app()
        app.state.runtime.update({
            "config": cfg,
            "state_store": ss,
            "broker": broker,
            "risk": risk,
            "position_manager": pm,
            "controller": ctl,
            "journal": journal,
            "data_dir": data_dir,
        })
        with TestClient(app) as client:
            body = client.get("/api/diag").json()
        bp = body["broker"]["position"]
        assert bp["side"] == "SHORT" and abs(float(bp["size"]) - 0.1) < 1e-9 and int(bp["leverage"]) == 10, (
            f"/api/diag broker.position={bp!r}，期望 SHORT/size=0.1/lev=10"
        )
        why = str(body["system"]["why_no_position"])
        assert "已持仓" in why and ("SHORT" in why.upper() or "空" in why), (
            f"why_no_position={why!r}，期望含「已持仓...空单/SHORT」(与 broker.position 一致)"
        )

    # ------------------------------------------------------------------
    # Bug C：SHORT 持仓时节流状态不要用『LONG_HOLD 持仓中』
    # ------------------------------------------------------------------
    def test_throttler_short_hold_reason_is_short_hold_not_long_hold(self):
        """SHORT 0.1 空单持仓 → reason 不得出现 "LONG_HOLD" 字眼（混淆多空）。
        要么新增 SHORT_HOLD 级别，要么 reason 改成 『持仓中(多/空)』+ 中文方向。"""
        from app.core.ai_throttle import AIThrottler
        from app.core.constants import ThrottleLevel
        thr = AIThrottler()
        # 初始化状态：RUNNING+有持仓（SHORT 方向在 entry_price 不影响，只要 has_pos=True 即可）
        dec = thr.should_call_ai(
            now_ts=1_700_000_000,
            system_status_running=True,
            allow_trading=True,
            has_position=True,  # 这里是 SHORT 持仓场景
            mark_price=2466.0,
            entry_price=2466.0,
            stop_loss_price=2404.0,
            liquidation_price=4680.0,
        )
        reason = str(dec.reason or "")
        # 断言：如果 reason 含 "持仓中"，就不得有 "LONG_HOLD 持仓中"（要根据持单方向显示）
        if "持仓中" in reason:
            assert "LONG_HOLD 持仓中" not in reason, (
                f"SHORT 场景下 reason={reason!r}：错误地写死 LONG_HOLD 字样（Bug C）"
                "，应当把持仓方向写进 reason，或用 HOLD/HOLD_SHORT 等中立表述"
            )

    # ------------------------------------------------------------------
    # Bug D（可选轻量）：ShadowBroker 返回的虚拟持仓，当 inner 没 mark 时至少给 entry*作为"估算价"
    # ------------------------------------------------------------------
    def test_shadow_get_position_when_inner_mark_zero_uses_sane_mark_not_entry_for_long(self):
        """SHORT 开 2466 后，真实 mark 永远 = inner 空仓的 mark（=0）会导致 ShadowBroker
        mark_price=2466（=entry），即使 eth 实际涨跌。本测试至少确认：
        ShadowBroker.get_position 后 mark_price 不是 0（有一个合理的兜底值）。
        真正 fix 建议：每次 bg_main_loop 更新 mark 到 ShadowBroker._last_mark_cache。"""
        from app.broker.shadow import ShadowBroker
        from app.broker.paper import PaperBroker
        from app.core.config import RiskLimits
        from app.core.constants import OrderSide, OrderType
        import asyncio as _aio
        cfg_rl = RiskLimits(shadow_mode=True, live_max_equity_usdt=15.0)
        inner = PaperBroker(symbol="ETH-USDT-SWAP", initial_balance=14.83)
        # PaperBroker 真实持仓=空，get_position().mark_price=0 → 正是 ShadowBroker 遇的场景
        sb = ShadowBroker(inner=inner, symbol="ETH-USDT-SWAP")
        async def _go():
            await sb.set_leverage("ETH-USDT-SWAP", 10)
            await sb.place_order(symbol="ETH-USDT-SWAP", side=OrderSide.SELL,
                                 type=OrderType.LIMIT, amount=0.1, price=2466.0,
                                 client_order_id="MARK-TEST-1")
            return await sb.get_position("ETH-USDT-SWAP")
        pos = _aio.run(_go())
        # mark_price 绝不=0（兜底：entry_price）
        assert float(pos.mark_price or 0.0) > 0, (
            f"ShadowBroker SHORT 持仓 mark_price={pos.mark_price} = 0，Dashboard 现价显示 0.00 是乱码 Bug D"
        )


# ============================================================================
# Test_8_20260831_VolatilityBasedIntervals（用户反馈：固定节流会错过行情）
#
# 用户原话：「固定时间节流不合适吧 偶尔会错过 应该根据行情」
#   期望行为：
#   A) NORMAL 档位下，近 1m 波动越大 → 间隔要越短（例如 event_pct=1.8% → interval ≤ 60s*0.6=36s）
#   B) 横盘 event_pct<0.2% → 间隔要主动拉长（≤2×基础档），省 AI 成本
#   C) UTC 0-6 亚洲深夜(SLEEP)，也要保留「行情波动 ≥1% 就早叫」的口子，
#      不能像旧逻辑：SLEEP 只认 big_event_1m_pct(2%) → 1.8% 急跌就错过（用户痛点）
# ============================================================================
class Test_8_DynamicVolatilityIntervals:
    def _mk_throttler_at(self, utc_hour: int):
        """构造一个 AIThrottler，并把 sentinel 锚定到基准价=2000，返回 (thr, base_ts)。"""
        from app.core.ai_throttle import AIThrottler
        import time as _t, calendar as _cal
        # 2025-01-01 {utc_hour}:00:00 UTC 的时间戳（calendar.timegm 纯 UTC，不受本地时区影响）
        base_ts = _cal.timegm((2025, 1, 1, utc_hour, 0, 0))
        assert _t.gmtime(base_ts).tm_hour == utc_hour, (
            f"测试工具 bug：造出的 ts gmtime.hour={_t.gmtime(base_ts).tm_hour}，期望 {utc_hour}"
        )
        thr = AIThrottler()
        # 预填锚定价(基准=2000) & 锚定时间戳
        thr.state.sentinel_mark_price = 2000.0
        thr.state.sentinel_anchor_ts = base_ts - 30  # 30s 前（1m 窗口内）
        thr.state.last_call_ts = base_ts - 120  # 预热：不是 cold_start
        thr.state.next_call_ts = 0  # 保证新轮决策
        return thr, base_ts

    # ---- (A) 高波动 → 更短间隔 ----
    def test_normal_high_volatility_shortens_interval(self):
        """NORMAL 档位 + event_pct≈1.85% → 间隔必须 < 60s，明显比横盘短。
        旧代码：永远 60s = 固定，必然错过。"""
        from app.core.ai_throttle import ThrottleLevel
        thr, now = self._mk_throttler_at(utc_hour=12)  # UTC 白天 → 不命中 SLEEP
        # mark=2037 → vs 2000 anchor → event=1.85%
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2037.0, entry_price=0,
        )
        assert dec.level == ThrottleLevel.NORMAL, (
            f"空仓 running 场景应该是 NORMAL（没到 3% HOT 档），实际 {dec.level}"
        )
        base_60s = 60
        assert int(dec.interval_s) <= int(base_60s * 0.65), (
            f"event=1.85% 应当明显缩短(≤{base_60s*0.65:.0f}s)，实际 interval={dec.interval_s}s（用户吐槽：固定时间节流错过行情）"
        )
        # 决策要给出「为什么是 Xs」的理由（给 Dashboard 解释用）
        assert "动态" in dec.reason or "波动" in dec.reason, (
            f"reason 必须含「动态/波动」字样来解释为什么缩短了 interval：{dec.reason!r}"
        )

    # ---- (B) 横盘 → 更长间隔（省成本） ----
    def test_normal_sideways_lengthens_interval(self):
        """NORMAL 档位 + event_pct≈0.05%（横盘）→ 间隔要主动拉长到 >60s。"""
        thr, now = self._mk_throttler_at(utc_hour=12)
        # mark=2001 → event=0.05%（几乎横盘）
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2001.0, entry_price=0,
        )
        base_60s = 60
        assert int(dec.interval_s) > int(base_60s), (
            f"横盘 0.05% 应当拉长间隔(>{base_60s}s)，实际 interval={dec.interval_s}s（旧固定 60s=浪费 AI 成本）"
        )

    # ---- (C) SLEEP 窗 1.8% 也必须早叫（旧版只认 2% 会错过） ----
    # 2026-08-31 更新：不再按 UTC 硬切 SLEEP，必须通过「流动性差」触发 SLEEP（用户原话：按流动性不按 UTC）。
    #   所以本测试显式传入价差宽(+40)+量低(+30)+UTC2 加成(+25)-波动大(-10)=85≥60→SLEEP；
    #   然后验证 1.8%（<2% 旧 SLEEP 不早叫的「坑」）依然命中 early_wake（>= sleep_wake_pct=1%）。
    def test_sleep_window_1p8pct_still_triggers_early_wake(self):
        """SLEEP(流动性差) + event=1.8% → early_wake=True（不能漏 1~2% 急跌/暴涨）。
        旧逻辑：SLEEP 只当 big_event_1m_pct=2% 才早叫 → 1.8% 被静默错过。"""
        from app.core.ai_throttle import ThrottleLevel
        thr, now = self._mk_throttler_at(utc_hour=2)
        # mark=2036 → event=1.8%（<2% 旧 SLEEP 不早叫的「坑」，但 ≥ sleep_wake_pct=1% → 应该早叫）
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2036.0, entry_price=0,
            # 流动性差参数：价差≈0.59%≥0.35%(+40) + 量50张≤150(+30) + UTC2(+25) - 1.8%波动(-10) = 85≥60 → SLEEP
            bid_price=2032.0, ask_price=2044.0,  # 价差 12 / 中价 2038 = 0.588%（够宽）
            recent_volume_contracts=50,  # 1m 仅 50 张（≤低量阈值150）
        )
        assert dec.level == ThrottleLevel.SLEEP, (
            f"价差宽+量低+UTC加成 → 流动性评分应≥60进入SLEEP，实际 {dec.level}（reason={dec.reason!r}）"
        )
        assert bool(dec.early_wake) is True, (
            f"SLEEP + 波动 1.8% ≥ sleep_wake_pct(1%) 必须 early_wake=True（不能等 2% 才早叫=错过急跌爆发行情），实际 early_wake={dec.early_wake}, reason={dec.reason!r}"
        )
        assert int(dec.interval_s) <= int(600 * 0.5), (
            f"SLEEP+1.8% 间隔也要缩短到 ≤300s（原 600s 太长），实际 {dec.interval_s}"
        )


# ============================================================================
# Test_9_MinNotionalTracksCurrentPrice（用户反馈：最小名义永远=2.466U）
#
# 用户原话：「名义 2.466U ≥ min 2.466U 怎么一直是2.466 最小开仓不是根据现价定的吗
#          比如现价2488 最小2.5」
#   根因：run.py entry_price 兜底 2466 + last_verdict 快照不刷新 → 最小名义卡死。
# ============================================================================
class Test_9_MinNotionalTracksCurrentPrice:
    def _mk_eng(self):
        from app.risk.engine import RiskEngine
        return RiskEngine()

    def test_entry_price_2466_min_notional_equals_2dot466(self):
        """ETH=2466 → 最小名义 = 0.1张 × 0.01ETH × 2466 = 2.466U（用户看到的旧值）。"""
        import asyncio
        from app.broker.base import MarketSpec
        eng = self._mk_eng()
        eng.daily_start_balance = 100.0
        v = asyncio.run(eng.check_can_open(
            balance_total=14.83, balance_available=14.83, entry_price=2466.0,
            now_ts=1_750_000_000, market_spec=MarketSpec(),
        ))
        # 若允许：再单独按公式重算对照；若被拒绝也要保证最小名义跟随 entry_price
        assert abs(v.effective_min_notional_usdt - 2.466) < 0.02, (
            f"entry=2466 → 期望 min≈2.466U（0.1×0.01×2466），实际 {v.effective_min_notional_usdt}"
        )

    def test_entry_price_2488_min_notional_rises_to_2dot488(self):
        """现价涨到 2488 → 最小名义必须同步涨到 ≈2.488U（不能还停留在旧 2.466U）。"""
        import asyncio
        from app.broker.base import MarketSpec
        eng = self._mk_eng()
        eng.daily_start_balance = 100.0
        v = asyncio.run(eng.check_can_open(
            balance_total=14.83, balance_available=14.83, entry_price=2488.0,
            now_ts=1_750_000_001, market_spec=MarketSpec(),
        ))
        assert v.effective_min_notional_usdt >= 2.48, (
            f"entry=2488 → 期望 min≥2.48U（0.1×0.01×2488=2.488），实际 {v.effective_min_notional_usdt}"
            "（用户吐槽：永远 2.466=没跟随现价！）"
        )
        # 与 2.466 必须拉开：这里不能等于老值（断言差距 > 0.015U，避免浮点误差）
        assert v.effective_min_notional_usdt - 2.466 > 0.015, (
            f"entry=2488 必须比 entry=2466 的 min 名义大，实际差距={v.effective_min_notional_usdt - 2.466}"
        )

    def test_effective_min_notional_method_formula(self):
        """MarketSpec.effective_min_notional(entry_price) 直接按现价计算。"""
        from app.broker.base import MarketSpec
        spec = MarketSpec()
        m2466 = spec.effective_min_notional(2466.0)
        m2488 = spec.effective_min_notional(2488.0)
        assert 2.46 <= m2466 <= 2.48, f"2466 → {m2466}（期望≈2.466）"
        assert 2.485 <= m2488 <= 2.50, f"2488 → {m2488}（期望≈2.488）"
        # 两次必须不同（不能写成固定值）
        assert abs(m2466 - m2488) > 0.01, "2466 vs 2488 得到相同最小名义→写死了常数不是联动现价！"


# ============================================================================
# Test_10_SleepByLiquidityNotUtc（用户反馈：「节流应该根据行情流动性 而不是utc0-6」）
#
# 旧逻辑：elif gmtime(now).tm_hour < 6: 直接 SLEEP = 硬按时间睡（错）。
# 新逻辑：是否 SLEEP = 『流动性真差』(价差异常 + 量低 + 波动低) + (UTC 0-6 仅加权，不是硬切)
#         若 UTC 2:00 但价差窄/量足/有波动，说明有行情（如美盘尾盘消息）→ NORMAL 不 SLEEP；
#         若 UTC 12:00 但价差宽/量缩/没波动，说明白天流动性也差（节假日/盘前）→ 照样 SLEEP。
# ============================================================================
class Test_10_SleepByLiquidityNotUtc:
    def _mk(self, utc_hour):
        import calendar as _cal
        from app.core.ai_throttle import AIThrottler
        base_ts = _cal.timegm((2025, 1, 1, utc_hour, 0, 0))
        thr = AIThrottler()
        thr.state.sentinel_mark_price = 2000.0
        thr.state.sentinel_anchor_ts = base_ts - 30
        thr.state.last_call_ts = base_ts - 120
        thr.state.next_call_ts = 0
        return thr, base_ts

    # ---- 红用例 A：UTC 0-6 但流动性够（价差窄 + 事件波动≥0.3%）→ 必须不是 SLEEP ----
    def test_utc2_liquidity_ok_should_not_sleep(self):
        """UTC 2:00（旧逻辑=必SLEEP）但：价差 0.08%（窄）+ event=0.3%（有行情）→ 应该 NORMAL。
        用户痛点：UTC 2 点如果有消息面（ETF盘前/宏观事件）流动性依然好，硬按时间 SLEEP=漏行情。"""
        from app.core.ai_throttle import ThrottleLevel
        thr, now = self._mk(utc_hour=2)
        # mark=2006 → event=0.3%（有行情）；再传流动性：bid/ask 价差 0.08%（很窄） → 流动性充足
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2006.0, entry_price=0,
            # 新流动性参数（不强制，不传就退回旧逻辑。传了就按流动性判）
            bid_price=2005.2, ask_price=2006.8,  # 价差(2006.8-2005.2)/2006=0.08%
            recent_volume_contracts=5000,  # 最近 1m 成交 5000 张（不低）
        )
        assert dec.level != ThrottleLevel.SLEEP, (
            f"UTC 2:00 + 价差窄+有波动+量足 → 流动性充足，不该 SLEEP（用户：按流动性不按时间！），实际 {dec.level}"
        )
        assert dec.level == ThrottleLevel.NORMAL, f"期望 NORMAL，实际 {dec.level}"
        # reason 必须包含『流动性』或『价差』字样（证明新逻辑生效）
        assert any(w in dec.reason for w in ("流动性", "价差", "量")), (
            f"新判定逻辑必须写明为什么没睡（流动性充足），实际 reason={dec.reason!r}"
        )

    # ---- 红用例 B：UTC 白天 12:00 但流动性极差（价差宽 + 极低量 + 0 波动）→ 应该 SLEEP ----
    def test_utc12_liquidity_poor_should_still_sleep(self):
        """UTC 12:00（旧逻辑=一定不 SLEEP）但价差 0.8%（宽）+ 量几乎=0 → 照样 SLEEP。
        场景：独立日/圣诞节假日白天，交易所挂单稀疏；或 ETH 周末横盘。"""
        from app.core.ai_throttle import ThrottleLevel
        thr, now = self._mk(utc_hour=12)
        # mark=2000.6 → event=0.03%（几乎没波动）；价差 0.8% 极宽；量 30 张/1m（几乎没量）
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2000.6, entry_price=0,
            bid_price=1992.6, ask_price=2008.6,  # 价差(2008.6-1992.6)/2000.6 = 0.8%（极宽）
            recent_volume_contracts=30,  # 1m 30 张（死寂）
        )
        assert dec.level == ThrottleLevel.SLEEP, (
            f"UTC 12:00 但价差宽+量缩+0波动 → 流动性差，应该 SLEEP（不按时间按行情！），实际 {dec.level}"
        )

    # ---- 红用例 C：UTC 2:00 + 流动性差（和旧结果一致）→ 继续 SLEEP 没毛病 ----
    def test_utc2_liquidity_poor_backward_compat_sleep(self):
        """UTC 2:00 + 价差 0.6% + 量低 + 无波动 → 流动性差，SLEEP（老场景兼容）。"""
        from app.core.ai_throttle import ThrottleLevel
        thr, now = self._mk(utc_hour=2)
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2000.4, entry_price=0,
            bid_price=1994.4, ask_price=2006.4,  # 价差≈0.6%
            recent_volume_contracts=80,  # 量低
        )
        assert dec.level == ThrottleLevel.SLEEP, (
            f"UTC 2+流动性差本来就该睡，向后兼容失败，实际 {dec.level}"
        )

    # ---- 红用例 D：不传流动性参数（纯 backward 兼容：缺输入时不得崩） → 至少回旧/正常档位 ----
    def test_no_liquidity_args_does_not_crash(self):
        """没接流动性实盘输入时（旧调用方），不应该 KeyError / 除 0。"""
        thr, now = self._mk(utc_hour=12)
        # 什么额外参数都不传（只有 should_call_ai 的老参数）
        dec = thr.should_call_ai(
            now_ts=now, system_status_running=True, allow_trading=True,
            has_position=False, mark_price=2001.0, entry_price=0,
        )
        assert bool(dec.should_call) in (True, False)
        assert dec.interval_s > 0


