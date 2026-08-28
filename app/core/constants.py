# -*- coding: utf-8 -*-
"""全局枚举与常量定义（设计文档 4/20 节）。"""

from __future__ import annotations

from enum import Enum


# ---------------------------------------------------------------------------
# AI 输出：市场状态
# ---------------------------------------------------------------------------
class MarketRegime(str, Enum):
    """AI 分析输出的市场状态（设计文档 · 第四节）。"""
    TREND_UP = "TREND_UP"           # 趋势向上
    TREND_DOWN = "TREND_DOWN"       # 趋势向下
    RANGE = "RANGE"                 # 震荡
    HIGH_VOLATILITY = "HIGH_VOLATILITY"  # 高波动
    LOW_VOLATILITY = "LOW_VOLATILITY"    # 低波动


# ---------------------------------------------------------------------------
# 系统运行状态
# ---------------------------------------------------------------------------
class SystemStatus(str, Enum):
    """系统运行状态（设计文档 · 第二十节）。"""
    RUNNING = "RUNNING"       # 运行中
    STOPPED = "STOPPED"       # 已停止
    RECOVERING = "RECOVERING" # 恢复中
    ERROR = "ERROR"           # 异常


class RunMode(str, Enum):
    """运行模式（纸盘 / 实盘）。"""
    PAPER = "PAPER"
    LIVE = "LIVE"


# ---------------------------------------------------------------------------
# 订单 / 持仓
# ---------------------------------------------------------------------------
class OrderSide(str, Enum):
    BUY = "BUY"     # 买入开多 / 平空
    SELL = "SELL"   # 卖出开空 / 平多


class OrderType(str, Enum):
    LIMIT = "LIMIT"      # 限价（Maker 优先）
    MARKET = "MARKET"    # 市价（止损专用）
    STOP = "STOP"        # 止损单


class OrderStatus(str, Enum):
    """订单状态（设计文档 · 第二十节）。"""
    PENDING = "PENDING"    # 待成交
    PARTIAL = "PARTIAL"    # 部分成交
    FILLED = "FILLED"      # 已成交
    CANCELED = "CANCELED"  # 已撤销
    ERROR = "ERROR"        # 异常


class PositionSide(str, Enum):
    LONG = "LONG"     # 做多
    SHORT = "SHORT"   # 做空
    FLAT = "FLAT"     # 空仓


# ---------------------------------------------------------------------------
# 业务常量
# ---------------------------------------------------------------------------
# 订单号前缀：YL-YYYYMMDD-XXXXX
CLIENT_ORDER_PREFIX = "YL"

# 单品种：ETH-USDT-SWAP
SYMBOL = "ETH-USDT-SWAP"

# 时间同步间隔（秒）：每 5 分钟
TIME_SYNC_INTERVAL = 5 * 60

# 时间漂移阈值（秒）：超过 10 秒暂停开仓
TIME_DRIFT_THRESHOLD = 10

# Maker 挂单等待超时（秒）：20 秒未成交则撤单重评
MAKER_WAIT_TIMEOUT = 20
