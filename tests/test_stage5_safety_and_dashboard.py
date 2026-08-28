"""
TDD：阶段 5 · 启动前安全自检 + Dashboard 首页面板
目标：
  · Safety：
    1) 实盘(live=true) 时，若 OKX / AI 密钥是占位字符串(如 YOUR_xxx) → raise RuntimeError
    2) 纸盘(live=false) 允许占位值（仅给 WARNING），不阻止启动
    3) paper 模式无需 OKX 密钥，但 AI 占位仍可启动
  · Dashboard：
    4) GET / 返回中文 HTML，200，含「云龙挑战赛」「运行模式」「余额」「风控」「持仓」「AI 判断」「最近交易」等关键文本块
    5) 响应头 content-type=text/html
"""
from __future__ import annotations

import pytest

from app.core.safety import validate_runtime_credentials


# ---------------------------------------------------------------------------
# RED 用例 1：实盘 + OKX 占位 → 抛错
# ---------------------------------------------------------------------------
def test_live_mode_rejects_placeholder_okx_key():
    with pytest.raises(RuntimeError) as exc:
        validate_runtime_credentials(
            live=True,
            okx_api_key="YOUR_OKX_API_KEY",
            okx_secret="abc-real-secret-xyz",
            okx_passphrase="real-pass",
            ai_api_key="sk-real-ai-key",
        )
    msg = str(exc.value)
    assert "实盘模式" in msg
    assert "okx.api_key" in msg or "OKX" in msg


def test_live_mode_rejects_placeholder_ai_key():
    with pytest.raises(RuntimeError) as exc:
        validate_runtime_credentials(
            live=True,
            okx_api_key="a-real-okx-key",
            okx_secret="a-real-secret",
            okx_passphrase="a-real-pass",
            ai_api_key="YOUR_AI_API_KEY",
        )
    msg = str(exc.value)
    assert "实盘模式" in msg
    assert "AI" in msg or "ai.api_key" in msg


def test_live_mode_rejects_empty_okx():
    with pytest.raises(RuntimeError):
        validate_runtime_credentials(
            live=True,
            okx_api_key="",
            okx_secret="",
            okx_passphrase="",
            ai_api_key="sk-valid",
        )


# ---------------------------------------------------------------------------
# RED 用例 2：纸盘 + 占位值 → 不抛错（仅记录 warning，返回 True）
# ---------------------------------------------------------------------------
def test_paper_mode_allows_placeholders(caplog):
    ok = validate_runtime_credentials(
        live=False,
        okx_api_key="YOUR_OKX_API_KEY",
        okx_secret="YOUR_OKX_API_SECRET",
        okx_passphrase="YOUR_OKX_PASSPHRASE",
        ai_api_key="YOUR_AI_API_KEY",
    )
    assert ok is True


def test_paper_mode_allows_real_values():
    ok = validate_runtime_credentials(
        live=False,
        okx_api_key="a-real-key",
        okx_secret="a-real-secret",
        okx_passphrase="a-real-pass",
        ai_api_key="sk-some-thing",
    )
    assert ok is True


# ---------------------------------------------------------------------------
# RED 用例 3：实盘 + 全真实 → 不抛错
# ---------------------------------------------------------------------------
def test_live_mode_real_keys_ok():
    ok = validate_runtime_credentials(
        live=True,
        okx_api_key="8a7b6c5d-1234-5678-abcd-ef1234567890",
        okx_secret="E7F8G9H0I1J2K3L4M5N6O7P8==",
        okx_passphrase="StrongPassphrase!1",
        ai_api_key="sk-deepseek-abcdefg1234567",
    )
    assert ok is True


# ---------------------------------------------------------------------------
# RED 用例 4：Dashboard GET / 返回中文 HTML 面板
# ---------------------------------------------------------------------------
def test_dashboard_root_returns_html_with_key_blocks():
    # 使用 FastAPI TestClient（与 /api/status 测试一致的 app 生成路径）
    from app.api.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(runtime={})
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)

    # 200 OK & HTML
    assert resp.status_code == 200, f"期望 200，实际={resp.status_code}"
    assert "text/html" in resp.headers.get("content-type", "").lower()

    html = resp.text
    # 关键文本块：标题、四大分类、最近交易
    for key in (
        "云龙挑战赛", "运行模式", "余 额", "风 控", "持 仓", "AI 判 断", "最 近 交 易",
    ):
        assert key in html, f"首页面板缺少关键字段: {key!r}"
    # 必须有 <table>（最近交易表格）和 <style>（CSS）
    assert "<style>" in html or '<link rel="stylesheet"' in html, "需要 CSS 样式"
    assert "<table" in html, "最近交易表格 <table> 未渲染"
    assert "<!doctype html>" in html.lower() or "<html" in html.lower(), "不是合法 HTML 文档"


def test_dashboard_root_includes_data_from_runtime():
    """若 runtime 注入了假 state，首页 HTML 里应能看到对应数值（余额、胜率 等）。"""
    from app.api.app import create_app
    from fastapi.testclient import TestClient
    from app.storage.state_store import StateStore

    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    store = StateStore(tmp)
    store.save({
        "balance": {"total": 2345.67, "available": 1800.0, "unrealized_pnl": 12.34},
        "stats": {"wins": 7, "losses": 3, "trades_opened": 11, "trades_closed": 10},
    })

    class FakeBroker:
        pass

    app = create_app(runtime={
        "config": None,
        "broker": FakeBroker(),
        "state_store": store,
    })
    client = TestClient(app)
    resp = client.get("/")
    html = resp.text
    # 余额 2345.67 → 至少展示为 2345（允许 .67 四舍五入 / 格式化）
    assert "2345" in html, f"首页未渲染余额 2345.67（HTML 片段：{html[:500]}）"
    # 胜场 7、败场 3
    assert "7" in html, "胜场数未在首页展示"
    assert "3" in html, "败场数未在首页展示"
    # 胜率 (7/10=70%) 关键字
    assert "70" in html or "胜率" in html, "胜率字段缺失"
