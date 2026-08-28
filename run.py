# =============================================================================
# 云龙挑战赛（YCS）系统启动入口（完整装配版）
# 用法：
#   cd /workspace
#   .venv/bin/python run.py          # 默认：加载 config.yaml，监听 0.0.0.0:8000
#   .venv/bin/python run.py --dev    # 开发模式：127.0.0.1
# =============================================================================

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

# 确保项目根目录可导入
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 代理清理（参考经验 448377：本机 Dashboard / uvicorn 不走代理）
# ---------------------------------------------------------------------------
_PROXY_VARS = [
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
]

def clear_proxy_for_local() -> None:
    """清空代理变量；保证本机请求（uvicorn / curl localhost）直连。"""
    existing = {k: os.environ.pop(k, None) for k in _PROXY_VARS}
    # 强制设置 NO_PROXY 覆盖本机与 OKX Test 内网域
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    cleaned = [k for k, v in existing.items() if v]
    if cleaned:
        # 这里不依赖 loguru，因为 logger 尚未初始化
        print(f"[bootstrap] 已清空代理变量: {', '.join(cleaned)}", flush=True)


clear_proxy_for_local()

import uvicorn
from loguru import logger


# ---------------------------------------------------------------------------
# 日志：system.log / trade.log / error.log（设计文档 · 第十八节）
# ---------------------------------------------------------------------------
def setup_logger() -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 清除默认 handler
    logger.remove()

    # 1) 控制台 INFO+
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level:<7}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        enqueue=True,
    )

    # 2) system.log：全量 DEBUG+，每日轮换 30 天保留
    logger.add(
        log_dir / "system.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )

    # 3) trade.log：只保留 extra.log_type == "trade" 的交易日志
    logger.add(
        log_dir / "trade.log",
        level="INFO",
        filter=lambda record: record["extra"].get("log_type") == "trade",
        rotation="00:00",
        retention="60 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )

    # 4) error.log：ERROR+
    logger.add(
        log_dir / "error.log",
        level="ERROR",
        rotation="00:00",
        retention="60 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )


# ---------------------------------------------------------------------------
# 主装配：加载配置 → 构建组件 → 恢复 → 后台任务
# ---------------------------------------------------------------------------
async def bootstrap_runtime(app) -> dict[str, Any]:
    """在 lifespan 内执行：加载 YAML 配置、构建所有组件并注入 app.state.runtime。

    Returns:
        dict 包含所有组件引用，便于后续管理。
    """
    from app.core.config import load_config, AppConfig
    from app.core.constants import SYMBOL
    from app.ai.factory import build_ai_provider
    from app.ai.base import AIProvider
    from app.broker.factory import build_broker
    from app.broker.base import Broker
    from app.risk.engine import RiskEngine
    from app.trading.position_manager import PositionManager
    from app.storage.state_store import StateStore
    from app.storage.trade_journal import TradeJournal
    from app.exchange.market import MarketDataProducer
    from app.recovery.recoverer import SystemRecoverer
    from app.services.controller import TradingController

    cfg_path = PROJECT_ROOT / "config.yaml"
    logger.info("读取配置: {}", cfg_path)
    try:
        cfg: AppConfig = load_config(cfg_path)
    except Exception:
        logger.exception("加载 config.yaml 失败，使用测试占位配置启动")
        from app.core.config import OKXConfig, AIConfig, TradingConfig
        cfg = AppConfig(
            okx=OKXConfig(api_key="", secret="", passphrase=""),
            ai=AIConfig(provider="deepseek", api_key="", model="deepseek-chat"),
            trading=TradingConfig(live=False, symbol=SYMBOL),
        )

    symbol = cfg.trading.symbol
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1) 存储层
    state_store = StateStore(data_dir)
    journal = TradeJournal(data_dir)

    # 2) 风控 / 仓位管理：恢复持久化状态
    risk = RiskEngine()
    risk.load_dict((state_store.load() or {}).get("risk"))
    position_manager = PositionManager()
    position_manager.load_dict((state_store.load() or {}).get("position_manager"))

    # 3) AI / Broker
    ai: AIProvider = build_ai_provider(cfg.ai)
    broker: Broker = build_broker(cfg)

    # 4) 行情生产者（纸盘 / 实盘都可使用；OKX 空密钥时仅测试用）
    market_producer = MarketDataProducer(okx=cfg.okx, symbol=symbol)

    # 5) TradingController（总控）
    controller = TradingController(
        config=cfg,
        broker=broker,
        ai=ai,
        risk=risk,
        state_store=state_store,
        journal=journal,
        market_producer=market_producer,
    )
    # 额外挂载 PositionManager（未来主交易循环用）
    setattr(controller, "position_manager", position_manager)

    # 6) SystemRecoverer：OKX 时间同步 + 余额/持仓/挂单 写回 state（交易所优先）
    recoverer = SystemRecoverer(broker=broker, state_store=state_store, symbol=symbol)
    # 若为纸盘模式，recover 会走 PaperBroker 的模拟余额 / 空仓 / 无挂单 → 正常写回 RUNNING
    try:
        recovered = await recoverer.recover()
        bal_total = float((recovered.get("balance") or {}).get("total", 0.0))
        if bal_total > 0 and risk.daily_start_balance <= 0:
            risk.start_new_day(bal_total)
        # 写回风控 / 仓位管理器持久态
        st = state_store.load()
        st.setdefault("risk", {}).update(risk.to_dict())
        st.setdefault("position_manager", {}).update(position_manager.to_dict())
        state_store.save(st)
    except Exception:
        logger.exception("启动恢复失败，以 STOPPED 继续启动 Dashboard（请检查 OKX 网络/密钥）")

    # 7) 注入 FastAPI runtime
    app.state.runtime.update({
        "config": cfg,
        "broker": broker,
        "ai": ai,
        "risk": risk,
        "position_manager": position_manager,
        "storage": (state_store, journal),
        "market_producer": market_producer,
        "recoverer": recoverer,
        "controller": controller,
    })

    logger.success("运行时组件装配完成：模式={} symbol={}", cfg.trading.mode.value, symbol)
    return {
        "config": cfg, "broker": broker, "ai": ai, "risk": risk,
        "position_manager": position_manager, "state_store": state_store,
        "journal": journal, "market_producer": market_producer,
        "recoverer": recoverer, "controller": controller,
    }


# ---------------------------------------------------------------------------
# 后台任务：时间同步 + 交易主循环（占位 + 利润保护轮询）
# ---------------------------------------------------------------------------
async def bg_time_sync(rt: dict[str, Any]) -> None:
    """每 TIME_SYNC_INTERVAL 秒同步一次 OKX 时间（设计文档 · 第十四节）。"""
    from app.core.constants import TIME_SYNC_INTERVAL
    rec: Any = rt["recoverer"]
    while True:
        try:
            await rec.sync_time()
        except Exception:
            logger.exception("后台时间同步失败")
        await asyncio.sleep(TIME_SYNC_INTERVAL)


async def bg_main_loop(rt: dict[str, Any]) -> None:
    """交易主循环占位（阶段 4 实盘验证时填充）。

    当前占位功能：
      - 每 30 秒调用一次 PositionManager 利润保护检查（若已有持仓则触发 should_close_for_protection）
      - 每次写入 state.json 的 risk / position_manager 持久态
      - 【未来扩展】：拉行情 → analyze() AI → check_can_open() 风控 → Maker 先挂单 20s → 止损止盈
    """
    from app.core.constants import SystemStatus
    while True:
        try:
            state_store = rt["state_store"]
            risk = rt["risk"]
            pm = rt["position_manager"]
            ctl = rt["controller"]
            st = state_store.load()

            # 持久化：risk / position_manager
            st["risk"] = risk.to_dict()
            st.setdefault("position_manager", {}).update(pm.to_dict())

            # 若系统运行，则做一次利润保护轮询（读取 position）
            if st.get("status") == SystemStatus.RUNNING.value:
                try:
                    pos = await ctl.broker.get_position(ctl.config.trading.symbol)
                    need_close, reason = pm.should_close_for_protection(pos)
                    if need_close:
                        logger.bind(log_type="trade").warning("利润保护触发：{}", reason)
                        # 阶段 4：这里真正下市价平仓单（调用 OrderManager / Broker）
                        # await ctl.broker.place_order(...)
                except Exception:
                    logger.exception("利润保护轮询异常")

            state_store.save(st)
        except Exception:
            logger.exception("主循环占位执行异常")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="云龙挑战赛（YCS）自动交易系统启动器")
    parser.add_argument("--dev", action="store_true", help="开发模式：监听 127.0.0.1:8000")
    parser.add_argument("--host", default=None, help="自定义监听 host（覆盖 --dev）")
    parser.add_argument("--port", type=int, default=8000, help="自定义监听 port，默认 8000")
    parser.add_argument("--timeout-keepalive", type=int, default=5, help="uv keepalive 秒数")
    args = parser.parse_args()

    setup_logger()
    logger.info("=" * 60)
    logger.info("云龙挑战赛系统（YCS）启动中 · 目录: {}", PROJECT_ROOT)
    logger.info("=" * 60)

    # 用 FastAPI lifespan 执行 bootstrap，避免 TestClient 触发真实装配
    runtime_holder: dict[str, Any] = {}

    async def _on_startup() -> None:
        rt = await bootstrap_runtime(app_instance_ref[0])
        runtime_holder.update(rt)
        # 创建后台任务（不阻塞 lifespan）
        loop = asyncio.get_event_loop()
        ts_task = loop.create_task(bg_time_sync(rt))
        loop_task = loop.create_task(bg_main_loop(rt))
        ts_task.set_name("ycs-bg-time-sync")
        loop_task.set_name("ycs-bg-main-loop")
        runtime_holder["_tasks"] = [ts_task, loop_task]
        logger.info("后台任务已启动: {}", [t.get_name() for t in runtime_holder["_tasks"]])

    async def _on_shutdown() -> None:
        tasks = runtime_holder.get("_tasks") or []
        for t in tasks:
            if not getattr(t, "done", lambda: False)():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("云龙挑战赛系统已优雅关闭")

    from app.api.app import create_app
    app_instance_ref = [None]
    app = create_app(
        config_path=PROJECT_ROOT / "config.yaml",
        on_startup=[_on_startup],
        on_shutdown=[_on_shutdown],
    )
    app_instance_ref[0] = app

    host = args.host or ("127.0.0.1" if args.dev else "0.0.0.0")
    port = args.port
    logger.info("拉起 FastAPI Dashboard: http://{}:{}/docs", host, port)

    # 清空代理后再启动 uvicorn（保证直连本机，无代理干扰）
    clear_proxy_for_local()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        timeout_keep_alive=args.timeout_keepalive,
    )


if __name__ == "__main__":
    main()
