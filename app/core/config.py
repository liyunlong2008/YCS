# -*- coding: utf-8 -*-
"""YAML 配置加载（设计文档 · 第七节）。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .constants import RunMode, SYMBOL


class OKXConfig(BaseModel):
    """OKX API 凭证。"""
    api_key: str
    secret: str
    passphrase: str


class AIConfig(BaseModel):
    """AI 提供商配置（LiteLLM 统一接入）。"""
    provider: Literal["deepseek", "openai", "claude", "gemini", "openrouter"]
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = ""


class TradingConfig(BaseModel):
    """交易运行模式配置。"""
    live: bool = False
    symbol: str = SYMBOL

    @property
    def mode(self) -> RunMode:
        return RunMode.LIVE if self.live else RunMode.PAPER


class RiskLimits(BaseModel):
    """实盘硬风控阈值（用户 2026-08-29：直接上实盘前按护栏方案补全，适配 14.8 USDT 超小账户）。

    护栏映射：
      A1. 本金上限硬锁 → live_max_equity_usdt
      A2. 每日亏损熔断（USDT 绝对值）→ live_max_daily_loss_usdt
      A3. 订单双因子 sanity → live_max_single_order_usdt + position_change_pct
      A5. Kill-Switch → kill_switch_token
      A7. Shadow 影子模式 → shadow_mode
    """
    live_max_equity_usdt: float = 15.0
    live_max_daily_loss_usdt: float = 3.0
    live_max_single_order_usdt: float = 2.0
    position_change_pct: float = 0.10
    kill_switch_token: str = "YCS_KILL_CHANGEME_32BYTES_RANDOM_STRING_PLEASE"
    shadow_mode: bool = False


class AppConfig(BaseModel):
    """应用根配置对象。"""
    okx: OKXConfig
    ai: AIConfig
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)


def load_config(config_path: str | Path) -> AppConfig:
    """从 YAML 文件加载并校验配置。

    Args:
        config_path: config.yaml 路径。

    Returns:
        校验后的 AppConfig 实例。

    Raises:
        FileNotFoundError: 配置文件不存在。
        ValidationError: 配置字段缺失或非法。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return AppConfig.model_validate(raw)
