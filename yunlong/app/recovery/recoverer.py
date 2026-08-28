# -*- coding: utf-8 -*-
"""系统恢复器：按设计文档第十七节的启动流程执行恢复。"""

from __future__ import annotations

import asyncio
import time
from typing import Tuple

from loguru import logger

from ..broker.base import Broker
from ..core.constants import (
    TIME_DRIFT_THRESHOLD,
    TIME_SYNC_INTERVAL,
    SystemStatus,
)
from ..storage.state_store import StateStore


class SystemRecoverer:
    """系统恢复 / 时间同步管理器（占位实现，阶段 2 填充）。"""

    def __init__(self, broker: Broker, state_store: StateStore) -> None:
        self._broker = broker
        self._state_store = state_store
        self.last_server_time_ms: int = 0
        self.drift_ms: int = 0

    # ------------------------------------------------------------------
    async def sync_time(self) -> Tuple[int, int]:
        """同步 OKX 服务器时间，返回 (服务器时间 ms, 漂移 ms)。

        若漂移绝对值 > TIME_DRIFT_THRESHOLD * 1000，标记系统暂停开仓。
        """
        server_ms = await self._broker.get_server_time_ms()
        local_ms = int(time.time() * 1000)
        drift = local_ms - server_ms
        self.last_server_time_ms = server_ms
        self.drift_ms = drift

        state = self._state_store.load()
        if abs(drift) > TIME_DRIFT_THRESHOLD * 1000:
            state["status"] = SystemStatus.STOPPED.value
            logger.warning("时间同步漂移过大 {:.1f}s，系统暂停开仓", drift / 1000)
        else:
            if state.get("status") == SystemStatus.STOPPED.value:
                state["status"] = SystemStatus.RUNNING.value
            logger.info("时间同步完成，本地-服务器漂移 {:.2f}ms", drift)

        self._state_store.save(state)
        return server_ms, drift

    # ------------------------------------------------------------------
    async def recover(self) -> dict:
        """执行完整启动恢复流程。返回恢复后的系统状态。"""
        logger.info("开始系统恢复流程（交易所状态优先）")
        state = self._state_store.load()
        state["status"] = SystemStatus.RECOVERING.value
        self._state_store.save(state)

        await self.sync_time()

        # TODO(阶段 2): 查询余额 / 持仓 / 未成交订单 → 覆盖本地 state
        # balance = await self._broker.get_balance()
        # position = await self._broker.get_position(SYMBOL)
        # orders = await self._broker.get_open_orders(SYMBOL)

        state["status"] = SystemStatus.RUNNING.value
        self._state_store.save(state)
        logger.info("恢复完成，系统状态：{}", state["status"])
        return state

    # ------------------------------------------------------------------
    async def loop_sync_time(self) -> None:
        """常驻协程：每 TIME_SYNC_INTERVAL 秒同步一次时间。"""
        while True:
            try:
                await self.sync_time()
            except Exception:
                logger.exception("定期时间同步失败")
            await asyncio.sleep(TIME_SYNC_INTERVAL)
