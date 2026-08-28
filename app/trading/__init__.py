# -*- coding: utf-8 -*-
"""交易核心模块：OrderManager / PositionManager / Controller。

设计文档：
  - 第九节  OrderManager（订单生命周期、client_order_id 幂等）
  - 第十一节 仓位规则（单仓位）
  - 第十二节 成交规则（Maker First，20s 超时）
  - 第十三节 利润保护（阶梯移动止损）
"""

from .order_manager import OrderManager
from .position_manager import PositionManager, TrailingProfitConfig

__all__ = ["OrderManager", "PositionManager", "TrailingProfitConfig"]
