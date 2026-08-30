# -*- coding: utf-8 -*-
"""系统恢复器（设计文档 · 第十四节 / 第十七节）。

启动流程：读取配置 → 同步时间 → 连接 OKX → 查询余额 → 查询持仓
       → 查询订单 → 恢复状态 → 开始运行

恢复原则：交易所状态优先（覆盖本地 state.json）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Tuple

from loguru import logger

from ..broker.base import Broker
from ..core.constants import (
    SYMBOL,
    TIME_DRIFT_THRESHOLD,
    TIME_SYNC_INTERVAL,
    SystemStatus,
)
from ..storage.state_store import StateStore


class SystemRecoverer:
    """系统恢复 / 时间同步管理器。"""

    def __init__(self, broker: Broker, state_store: StateStore, symbol: str = SYMBOL) -> None:
        self._broker = broker
        self._state_store = state_store
        self._symbol = symbol
        self.last_server_time_ms: int = 0
        self.drift_ms: int = 0
        self._last_sync_ok: bool = False

    # ------------------------------------------------------------------
    # 时间同步（设计文档 · 第十四节）
    # ------------------------------------------------------------------
    async def sync_time(self) -> Tuple[int, int]:
        """同步交易所服务器时间。

        Returns:
            (服务器时间毫秒, 本地-服务器漂移毫秒)
        漂移绝对值超过阈值时，系统切换到 `STOPPED`（暂停开仓）。
        """
        server_ms = await self._broker.get_server_time_ms()
        local_ms = int(time.time() * 1000)
        drift_ms = local_ms - server_ms
        self.last_server_time_ms = server_ms
        self.drift_ms = drift_ms
        self._last_sync_ok = True

        st = self._state_store.load()
        drift_s = abs(drift_ms) / 1000
        st.setdefault("time_sync", {})
        st["time_sync"].update({
            "server_ms": server_ms,
            "local_ms": local_ms,
            "drift_ms": drift_ms,
            "last_sync_at": int(time.time()),
        })
        if drift_s > TIME_DRIFT_THRESHOLD:
            st["status"] = SystemStatus.STOPPED.value
            logger.warning("时间漂移 {:.2f}s，超过阈值 {}s；系统置为暂停开仓",
                           drift_s, TIME_DRIFT_THRESHOLD)
        else:
            # 仅在之前由「漂移超限」置 STOPPED 的情况下恢复为 RUNNING；
            # 其它 STOPPED 由人手动决定。
            if (st.get("status") == SystemStatus.STOPPED.value
                    and st["time_sync"].get("drifted_pause")):
                st["status"] = SystemStatus.RUNNING.value
                st["time_sync"]["drifted_pause"] = False
            st["time_sync"]["drifted_pause"] = drift_s > TIME_DRIFT_THRESHOLD
            logger.info("时间同步完成，漂移 {:.2f}ms（阈值 {}s）", drift_ms, TIME_DRIFT_THRESHOLD)
        self._state_store.save(st)
        return server_ms, drift_ms

    # ------------------------------------------------------------------
    # 状态恢复（设计文档 · 第十七节 · 交易所状态优先）
    # ------------------------------------------------------------------
    async def recover(self) -> dict:
        """按顺序执行完整恢复：时间 → 余额 → 持仓 → 挂单 → 覆盖 state。"""
        logger.info("=== 系统恢复开始（交易所状态优先）===")
        st = self._state_store.load()
        st["status"] = SystemStatus.RECOVERING.value
        # 统一 started_at 为「epoch 秒整数」便于 uptime 计算；避免与 /api/diag uptime_seconds 做减法时类型不一致
        existing = st.get("started_at")
        if isinstance(existing, str):
            # 兼容老格式「YYYY-MM-DD HH:MM:SS」字符串 → 反解为 epoch（老数据迁移一次）
            try:
                import datetime as _dt
                st["started_at"] = int(_dt.datetime.strptime(existing, "%Y-%m-%d %H:%M:%S").timestamp())
            except Exception:  # noqa: BLE001
                st["started_at"] = int(time.time())
        elif not isinstance(existing, int) or existing <= 0:
            st["started_at"] = int(time.time())
        self._state_store.save(st)

        # 1) 时间同步（若失败则进入异常）
        try:
            await self.sync_time()
        except Exception:
            logger.exception("时间同步失败")
            st["status"] = SystemStatus.ERROR.value
            self._state_store.save(st)
            return st

        # 2) 余额
        try:
            bal = await self._broker.get_balance()
            st["balance"] = {
                "total": bal.total,
                "available": bal.available,
                "unrealized_pnl": bal.unrealized_pnl,
            }
            logger.info("恢复余额: total={} available={} upl={}",
                        bal.total, bal.available, bal.unrealized_pnl)
        except Exception:
            logger.exception("查询余额失败")

        # 3) 持仓
        try:
            pos = await self._broker.get_position(self._symbol)
            st["position"] = pos.model_dump(mode="json")
            logger.info("恢复持仓: side={} size={} entry={}",
                        pos.side.value, pos.size, pos.entry_price)
        except Exception:
            logger.exception("查询持仓失败")

        # 4) 挂单
        try:
            orders = await self._broker.get_open_orders(self._symbol)
            st["open_orders"] = [o.model_dump(mode="json") for o in orders]
            logger.info("恢复挂单: {} 条", len(orders))
        except Exception:
            logger.exception("查询挂单失败")
            st["open_orders"] = []

        # 若时间同步无异常，置为 RUNNING
        if self._last_sync_ok and st["status"] != SystemStatus.ERROR.value:
            st["status"] = SystemStatus.RUNNING.value
        self._state_store.save(st)
        logger.info("=== 系统恢复完成，状态: {} ===", st["status"])
        return st

    # ------------------------------------------------------------------
    # 后台同步
    # ------------------------------------------------------------------
    async def loop_sync_time(self) -> None:
        """每 TIME_SYNC_INTERVAL 秒同步一次时间。应在 run.py 启动时作为 task create_task。"""
        while True:
            try:
                await self.sync_time()
            except Exception:
                logger.exception("定期时间同步失败")
            await asyncio.sleep(TIME_SYNC_INTERVAL)
