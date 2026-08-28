# -*- coding: utf-8 -*-
"""测试：阶段 1 的核心模块（config / 常量 / storage / 风控 / 仓位利润保护 / OrderManager）。

按 TDD 红-绿-重构：先写测试再补实现。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import yaml

from app.core.config import AppConfig, OKXConfig, AIConfig, TradingConfig, load_config
from app.core.constants import (
    CLIENT_ORDER_PREFIX,
    MAKER_WAIT_TIMEOUT,
    MarketRegime,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    RunMode,
    SystemStatus,
    TIME_DRIFT_THRESHOLD,
    TIME_SYNC_INTERVAL,
)
from app.broker.base import Balance, Order, Position
from app.broker.factory import build_broker
from app.risk.engine import RiskEngine, RiskVerdict
from app.storage.state_store import StateStore
from app.storage.trade_journal import TradeJournal, TradeRecord
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager, TrailingProfitConfig


# ---------------------------------------------------------------------------
# core/config 与常量
# ---------------------------------------------------------------------------
def test_load_config_from_yaml(tmp_path: Path) -> None:
    """能从 YAML 正确解析 OKX / AI / Trading 三段配置。"""
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump({
            "okx": {"api_key": "K", "secret": "S", "passphrase": "P"},
            "ai": {"provider": "deepseek", "api_key": "AK", "model": "deepseek-chat"},
            "trading": {"live": False, "symbol": "ETH-USDT-SWAP"},
        }),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.okx.api_key == "K"
    assert cfg.ai.provider == "deepseek"
    assert cfg.ai.model == "deepseek-chat"
    assert cfg.trading.live is False
    assert cfg.trading.mode == RunMode.PAPER
    assert cfg.trading.symbol == "ETH-USDT-SWAP"


def test_load_config_live_mode(tmp_path: Path) -> None:
    """live: true 时 mode == RunMode.LIVE。"""
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump({
            "okx": {"api_key": "K", "secret": "S", "passphrase": "P"},
            "ai": {"provider": "openai", "api_key": "X", "model": "gpt-5"},
            "trading": {"live": True},
        }),
        encoding="utf-8",
    )
    assert load_config(p).trading.mode == RunMode.LIVE


def test_business_constants_match_design() -> None:
    """业务常量与设计文档保持一致。"""
    assert TIME_SYNC_INTERVAL == 5 * 60
    assert TIME_DRIFT_THRESHOLD == 10
    assert MAKER_WAIT_TIMEOUT == 20
    assert CLIENT_ORDER_PREFIX == "YL"
    assert set(m.value for m in MarketRegime) == {
        "TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "LOW_VOLATILITY",
    }


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------
def test_state_store_defaults_and_roundtrip(tmp_path: Path) -> None:
    """首次读取返回默认值；写入后能重新读回，且是原子写（.tmp→replace）。"""
    s = StateStore(tmp_path)
    st = s.load()
    assert st["status"] == SystemStatus.STOPPED.value
    assert st["stats"]["trades_total"] == 0
    st["status"] = SystemStatus.RUNNING.value
    st["stats"]["trades_total"] = 7
    s.save(st)
    # 目录中只出现 state.json，不应残留 .tmp
    files = {x.name for x in tmp_path.iterdir()}
    assert "state.json" in files
    assert "state.json.tmp" not in files
    reloaded = s.load()
    assert reloaded["status"] == SystemStatus.RUNNING.value
    assert reloaded["stats"]["trades_total"] == 7


def test_state_store_corrupt_returns_default(tmp_path: Path) -> None:
    """损坏的 state.json 应安全退回默认值，不抛异常。"""
    (tmp_path / "state.json").write_text("{invalid json", encoding="utf-8")
    st = StateStore(tmp_path).load()
    assert st["status"] == SystemStatus.STOPPED.value
    assert st.get("balance", {}).get("total") == 0.0


# ---------------------------------------------------------------------------
# TradeJournal（JSONL）
# ---------------------------------------------------------------------------
def test_trade_journal_append_and_read_all(tmp_path: Path) -> None:
    j = TradeJournal(tmp_path)
    j.append(TradeRecord(
        market_regime=MarketRegime.TREND_UP, confidence=88,
        entry_reason="趋势突破", result="+2.5R",
    ))
    j.append(TradeRecord(
        market_regime=MarketRegime.RANGE, confidence=30,
        entry_reason="假突破反转", result="-1R",
    ))
    all_ = j.read_all()
    assert len(all_) == 2
    assert all_[0].market_regime == MarketRegime.TREND_UP
    assert all_[0].result == "+2.5R"
    # 文件是标准 JSONL（每行一个 JSON）
    lines = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # 每一行必须是合法 JSON


# ---------------------------------------------------------------------------
# 风控：连续亏损熔断 / 日亏损
# ---------------------------------------------------------------------------
def test_risk_consecutive_losses_circuit_breaker() -> None:
    """连亏 3 次 → 下一单被拒绝并进入 12h 冷却；冷却结束前继续拒绝。"""
    eng = RiskEngine()
    now = 1_700_000_000
    for _ in range(3):
        r = asyncio.run(eng.check_can_open(balance_total=1000, current_pnl_pct=0, now_ts=now))
        assert r.allow is True
        eng.on_trade_closed(pnl_pct=-0.5)  # 每次亏 0.5%，累计 3 次

    # 第 4 次请求前：consecutive_losses == 3 → 自动熔断
    banned = asyncio.run(eng.check_can_open(balance_total=1000, current_pnl_pct=0, now_ts=now))
    assert banned.allow is False
    assert "熔断" in banned.reason
    # cooldown_until_ts 至少 12h 之后
    assert eng.cooldown_until_ts >= now + 12 * 3600
    # 冷却期内仍然拒绝
    still_banned = asyncio.run(eng.check_can_open(
        balance_total=1000, current_pnl_pct=0, now_ts=now + 11 * 3600,
    ))
    assert still_banned.allow is False
    # 冷却期满，恢复放行
    ok = asyncio.run(eng.check_can_open(
        balance_total=1000, current_pnl_pct=0, now_ts=now + 13 * 3600,
    ))
    assert ok.allow is True


def test_risk_daily_loss_fuse() -> None:
    """日亏损 ≥ 15% → 直接拒绝开仓。"""
    eng = RiskEngine()
    eng.daily_start_balance = 1000.0
    # 亏到 849 → 15.1%
    r = asyncio.run(eng.check_can_open(
        balance_total=849, current_pnl_pct=-15.1, now_ts=1_700_000_000,
    ))
    assert r.allow is False
    assert "15%" in r.reason


def test_risk_win_resets_consecutive_losses() -> None:
    """盈利应清零连续亏损计数。"""
    eng = RiskEngine()
    eng.on_trade_closed(-1)
    eng.on_trade_closed(-0.1)
    assert eng.consecutive_losses == 2
    eng.on_trade_closed(+3.0)
    assert eng.consecutive_losses == 0


# ---------------------------------------------------------------------------
# 利润保护阶梯
# ---------------------------------------------------------------------------
def _pos(side: PositionSide, entry: float, mark: float, leverage: int = 3) -> Position:
    return Position(
        symbol="ETH-USDT-SWAP", side=side, size=1.0,
        entry_price=entry, mark_price=mark, leverage=leverage,
    )


def test_profit_trailing_long_steps() -> None:
    """做多：3 档阶梯触发后，锁盈只能上升。"""
    pm = PositionManager(TrailingProfitConfig.default())
    # 入场 2000 → 涨到 2020，+3% (3x 杠杆)
    assert abs(pm.calc_unrealized_pnl_pct(_pos(PositionSide.LONG, 2000, 2020, 3)) - 3.0) < 1e-6
    # 盈利 3% → 止损保本 0%
    lock = pm.get_required_stop_pct(_pos(PositionSide.LONG, 2000, 2020, 3))
    assert abs(lock - 0.0) < 1e-9
    # 盈利到 8% → 锁 3%
    mark_8pct = 2000 * (1 + 0.081 / 3)  # 8.1% 确保稳稳跨过 8% 门槛（避免浮点刚好贴边）
    pnl_8 = pm.calc_unrealized_pnl_pct(_pos(PositionSide.LONG, 2000, mark_8pct, 3))
    assert pnl_8 >= 8.0
    lock = pm.get_required_stop_pct(_pos(PositionSide.LONG, 2000, mark_8pct, 3))
    assert lock >= 3.0 - 1e-9
    # 回落到 4% 时，锁定值不会后退（ratchet），仍然 ≥3%
    mark_4pct = 2000 * (1 + 0.041 / 3)
    pnl_4 = pm.calc_unrealized_pnl_pct(_pos(PositionSide.LONG, 2000, mark_4pct, 3))
    assert 4.0 <= pnl_4 < 8.0  # 仍在第 2 档之下
    lock2 = pm.get_required_stop_pct(_pos(PositionSide.LONG, 2000, mark_4pct, 3))
    assert lock2 >= 3.0 - 1e-9


def test_profit_trailing_short() -> None:
    """做空：浮盈计算方向正确。"""
    pm = PositionManager(TrailingProfitConfig.default())
    # 入场 2000 → 跌到 1980，杠杆 3x → 浮盈 +3%
    pnl = pm.calc_unrealized_pnl_pct(_pos(PositionSide.SHORT, 2000, 1980, 3))
    assert abs(pnl - 3.0) < 1e-6
    lock = pm.get_required_stop_pct(_pos(PositionSide.SHORT, 2000, 1980, 3))
    assert abs(lock - 0.0) < 1e-9


def test_profit_trailing_flat_returns_zero() -> None:
    """空仓无浮盈。"""
    pm = PositionManager()
    p = Position(symbol="ETH-USDT-SWAP", side=PositionSide.FLAT)
    assert pm.calc_unrealized_pnl_pct(p) == 0.0


# ---------------------------------------------------------------------------
# OrderManager：client_order_id 幂等格式
# ---------------------------------------------------------------------------
def test_client_order_id_format() -> None:
    """订单号格式必须是 YL-YYYYMMDD-XXXXX，可指定日期 + seq。"""
    # 2026-08-28
    ts = time.mktime((2026, 8, 28, 0, 0, 0, 0, 0, -1))
    cid = OrderManager.generate_client_order_id(ts=ts, seq=1)
    assert cid == "YL-20260828-00001"
    cid = OrderManager.generate_client_order_id(ts=ts, seq=42)
    assert cid == "YL-20260828-00042"


# ---------------------------------------------------------------------------
# build_broker 工厂：paper / live 选择正确
# ---------------------------------------------------------------------------
def test_build_broker_paper_by_default() -> None:
    """live=False 返回 PaperBroker，无需 OKX 密钥。"""
    cfg = AppConfig(
        okx=OKXConfig(api_key="", secret="", passphrase=""),
        ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat"),
        trading=TradingConfig(live=False),
    )
    from app.broker.paper import PaperBroker
    assert isinstance(build_broker(cfg), PaperBroker)


def test_build_broker_live_requires_okx_keys() -> None:
    """live=True 但 OKX 密钥为空 → 断言失败。"""
    cfg = AppConfig(
        okx=OKXConfig(api_key="", secret="", passphrase=""),
        ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat"),
        trading=TradingConfig(live=True),
    )
    with pytest.raises(AssertionError):
        build_broker(cfg)


def test_build_broker_live_ok_when_keys_present() -> None:
    """live=True + 密钥齐全 → 返回 OKXBroker。"""
    cfg = AppConfig(
        okx=OKXConfig(api_key="K", secret="S", passphrase="P"),
        ai=AIConfig(provider="deepseek", api_key="X", model="deepseek-chat"),
        trading=TradingConfig(live=True),
    )
    from app.broker.okx_broker import OKXBroker
    assert isinstance(build_broker(cfg), OKXBroker)
