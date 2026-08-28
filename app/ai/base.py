# -*- coding: utf-8 -*-
"""AI 提供者统一抽象。

业务代码永远依赖 AIProvider，不依赖 DeepSeekClient / OpenAIClient。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..core.constants import MarketRegime


class MarketData(BaseModel):
    """输入给 AI 的市场数据快照。

    仅用于分析，不直接触发交易（风控 + Controller 拥有最终决定权）。
    """
    symbol: str
    timestamp: int = 0                     # 毫秒时间戳
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    ohlcv_1h: list[list[float]] = Field(default_factory=list)   # 近 N 根 1H K 线
    ohlcv_15m: list[list[float]] = Field(default_factory=list)  # 近 N 根 15m K 线
    extra: dict[str, Any] = Field(default_factory=dict)


class MarketAnalysisResult(BaseModel):
    """AI 分析输出（设计文档 · 第四节）。"""
    market_regime: MarketRegime
    confidence: int = Field(..., ge=0, le=100, description="置信度 0-100")
    reason: str = ""


class AIProvider(ABC):
    """AI 分析接口统一抽象。子类：DeepSeek / OpenAI / Claude / Gemini / OpenRouter。"""

    @abstractmethod
    async def analyze_market(self, market_data: MarketData) -> MarketAnalysisResult:
        """分析当前市场，输出市场状态。

        注意：AI 永远无权决定开仓 / 平仓 / 止损。
        """
        raise NotImplementedError
