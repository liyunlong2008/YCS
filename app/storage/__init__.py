# -*- coding: utf-8 -*-
"""存储模块：JSON / JSONL（设计文档 · 第十五 / 十六节）。

- data/state.json   账户、持仓、系统状态
- data/trades.jsonl 全部交易记录 / Trade Journal
"""

from .state_store import StateStore
from .trade_journal import TradeJournal, TradeRecord

__all__ = ["StateStore", "TradeJournal", "TradeRecord"]
