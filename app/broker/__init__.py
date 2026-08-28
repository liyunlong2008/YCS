# -*- coding: utf-8 -*-
"""Broker 模块：统一成交接口 + PaperBroker + OKXBroker。

设计文档第八节：业务代码永远依赖 Broker，不依赖 ccxt。
"""

from .base import Broker, Balance, Position, Order
from .factory import build_broker

__all__ = [
    "Broker",
    "Balance",
    "Position",
    "Order",
    "build_broker",
]
