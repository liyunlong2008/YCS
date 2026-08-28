# -*- coding: utf-8 -*-
"""OKX 行情适配层（设计文档 · 第五节 AI 架构上游）。

职责：
  - 抓取 ticker + 1H / 15m K 线
  - 标准化为 MarketData（供 AIProvider.analyze_market 消费）
  - 可选 K 线截断（只传最近 N 根）
"""

from __future__ import annotations

from typing import Optional

import ccxt.pro as ccxt_pro
from loguru import logger

from ..ai.base import MarketData
from ..core.config import OKXConfig
from ..core.constants import SYMBOL


class MarketDataProducer:
    """OKX 行情生产器。"""

    def __init__(
        self,
        okx: OKXConfig,
        symbol: str = SYMBOL,
    ) -> None:
        self.symbol = symbol
        self._cfg = okx
        # 懒初始化（注入测试 Fake）
        self._exchange: Optional[ccxt_pro.okx] = None

    # ------------------------------------------------------------------
    def _ensure_client(self) -> ccxt_pro.okx:
        if self._exchange is None:
            self._exchange = ccxt_pro.okx({
                "apiKey": self._cfg.api_key,
                "secret": self._cfg.secret,
                "password": self._cfg.passphrase,
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
        return self._exchange

    # ------------------------------------------------------------------
    async def get_market_data(self, max_candles: int = 96) -> MarketData:
        """拉取最新 ticker + 1H/15m K 线，并组装成 MarketData。

        Args:
            max_candles: 保留每个 timeframe 最近多少根 K 线（默认 96 根）。
        """
        ex = self._ensure_client()
        ticker = await ex.fetch_ticker(self.symbol)
        ohlcv_1h = await ex.fetch_ohlcv(self.symbol, timeframe="1h", limit=max_candles * 4)
        ohlcv_15m = await ex.fetch_ohlcv(self.symbol, timeframe="15m", limit=max_candles * 4)

        # 取最近 max_candles 根
        ohlcv_1h = list(ohlcv_1h[-max_candles:])
        ohlcv_15m = list(ohlcv_15m[-max_candles:])

        ts = int(ticker.get("timestamp") or 0)
        md = MarketData(
            symbol=self.symbol,
            timestamp=ts,
            open=float(ticker.get("open") or 0),
            high=float(ticker.get("high") or 0),
            low=float(ticker.get("low") or 0),
            close=float(ticker.get("close") or ticker.get("last") or 0),
            volume=float(ticker.get("baseVolume") or ticker.get("quoteVolume") or 0),
            ohlcv_1h=[[float(x) for x in row] for row in ohlcv_1h],
            ohlcv_15m=[[float(x) for x in row] for row in ohlcv_15m],
            extra={
                "last": float(ticker.get("last") or 0),
                "bid": float(ticker.get("bid") or 0),
                "ask": float(ticker.get("ask") or 0),
                "quoteVolume": float(ticker.get("quoteVolume") or 0),
            },
        )
        logger.debug(
            "行情拉取完成: symbol={} ts={} O={} H={} L={} C={} V={}",
            md.symbol, md.timestamp, md.open, md.high, md.low, md.close, md.volume,
        )
        return md
