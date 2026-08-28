# -*- coding: utf-8 -*-
"""自动恢复模块（设计文档 · 第十七节）。

启动流程：
  读取配置 → 同步时间 → 连接 OKX → 查询余额 → 查询持仓
  → 查询订单 → 恢复状态 → 开始运行
"""

from .recoverer import SystemRecoverer

__all__ = ["SystemRecoverer"]
