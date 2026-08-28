# -*- coding: utf-8 -*-
"""TradeJournal 追加：在 TradeRecord 基础上封装「决策流水」便捷方法，

供 Controller / 风控 / 平仓后使用，避免各模块反复拼 TradeRecord。
"""

from __future__ import annotations

from typing import Any

from ..core.constants import MarketRegime
from .trade_journal import TradeJournal, TradeRecord


def append_market(
    self: TradeJournal,
    *,
    regime: MarketRegime,
    confidence: int,
    entry_reason: str,
    result: str,
    **extra: Any,
) -> None:
    """记录一笔「市场决策 + 交易结果」流水。"""
    self.append(TradeRecord(
        market_regime=regime,
        confidence=confidence,
        entry_reason=entry_reason,
        result=result,
        extra=dict(extra),
    ))


# 绑定为 TradeJournal 方法，使调用处可以 `journal.append_market(...)`
if not hasattr(TradeJournal, "append_market"):
    TradeJournal.append_market = append_market  # type: ignore[attr-defined]

__all__ = ["append_market"]
