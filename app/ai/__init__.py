# -*- coding: utf-8 -*-
"""AI 模块：统一 AIProvider 抽象与各模型实现。

设计文档第四节：AI 永远只做市场状态分析，不负责开平仓决策。
"""

from .base import AIProvider, MarketAnalysisResult, MarketData
from .factory import build_ai_provider

__all__ = [
    "AIProvider",
    "MarketAnalysisResult",
    "MarketData",
    "build_ai_provider",
]
