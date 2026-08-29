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
