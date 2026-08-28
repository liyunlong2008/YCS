# -*- coding: utf-8 -*-
"""核心模块：配置加载、常量、枚举、工具函数。"""

from .config import load_config, AppConfig
from .constants import (
    MarketRegime,
    SystemStatus,
    RunMode,
    OrderSide,
    OrderType,
    OrderStatus,
    PositionSide,
    CLIENT_ORDER_PREFIX,
    SYMBOL,
    TIME_SYNC_INTERVAL,
    TIME_DRIFT_THRESHOLD,
    MAKER_WAIT_TIMEOUT,
)

__all__ = [
    "load_config",
    "AppConfig",
    "MarketRegime",
    "SystemStatus",
    "RunMode",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "PositionSide",
    "CLIENT_ORDER_PREFIX",
    "SYMBOL",
    "TIME_SYNC_INTERVAL",
    "TIME_DRIFT_THRESHOLD",
    "MAKER_WAIT_TIMEOUT",
]
