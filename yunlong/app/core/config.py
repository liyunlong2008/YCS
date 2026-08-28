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


class AppConfig(BaseModel):
    """应用根配置对象。"""
    okx: OKXConfig
    ai: AIConfig
    trading: TradingConfig = Field(default_factory=TradingConfig)


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
