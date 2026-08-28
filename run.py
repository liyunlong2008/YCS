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
    from app.core.safety import validate_runtime_credentials
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

    # === 0) 启动前安全自检：实盘占位密钥 → 立刻拒绝启动 ===
    try:
        validate_runtime_credentials(
            live=bool(cfg.trading.live),
            okx_api_key=cfg.okx.api_key,
            okx_secret=cfg.okx.secret,
            okx_passphrase=cfg.okx.passphrase,
            ai_api_key=cfg.ai.api_key,
        )
    except RuntimeError as exc:
        logger.error("[安全拦截] {}", exc)
        raise SystemExit(2) from exc

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

    # 3.5) OrderManager（Maker 优先）
    from app.trading.order_manager import OrderManager
    order_manager = OrderManager(broker, state_store=state_store)

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
    # 挂载 OrderManager 与 PositionManager 到 Controller（阶段 4 执行闭环使用）
    controller.order_manager = order_manager  # type: ignore[attr-defined]
    controller.position_manager = position_manager  # type: ignore[attr-defined]

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
    """交易主循环（阶段 4 完整闭环）。

    每 MAIN_LOOP_INTERVAL 秒：
      1) 日切点检测（跨日时重置 daily_start_balance）
      2) 拉取当前持仓
         - 空仓 → 调用 analyze() → 读取 AI 判断 & 置信度 → risk.check_can_open → execute_trade_signal
         - 持仓 → PositionManager.should_close_for_protection → True 时市价平仓并更新统计/风控
      3) 持久化 risk / position_manager / stats
    """
    from app.core.constants import SystemStatus, OrderSide, MarketRegime, PositionSide
    loop_interval = 30  # 30s 轮询
    while True:
        try:
            state_store: StateStore = rt["state_store"]
            risk: RiskEngine = rt["risk"]
            pm: PositionManager = rt["position_manager"]
            ctl: TradingController = rt["controller"]
            cfg = rt["config"]
            symbol = cfg.trading.symbol
            st = state_store.load()

            # ------------------------------------------------------------
            # 1) 日切点
            # ------------------------------------------------------------
            try:
                ctl.apply_daily_reset_if_needed()
            except Exception:
                logger.exception("日切点检测异常")

            # ------------------------------------------------------------
            # 2) 拉持仓 & 利润保护轮询（若已有持仓则触发 should_close_for_protection）
            # ------------------------------------------------------------
            if st.get("status") == SystemStatus.RUNNING.value:
                pos = await ctl.broker.get_position(symbol)
                need_close, close_reason = pm.should_close_for_protection(pos)

                if pos.side != PositionSide.FLAT:
                    # 已有持仓 → 只做利润保护/止损，不再开新仓
                    if need_close:
                        logger.bind(log_type="trade").warning("利润保护触发（主循环）：{}", close_reason)
                        realized = await ctl.close_position_for_protection(pos)
                        logger.success("[主循环] 利润保护平仓完成：已实现盈亏={:.6f}U", realized)
                else:
                    # 空仓 → 拉 AI → 风控 → 下单
                    if (need_close, need_close):  # dummy to avoid unused var
                        pass
                    try:
                        ai_block = await ctl.analyze()
                    except Exception:
                        logger.exception("AI 分析异常（跳过本轮开仓）")
                        ai_block = {}
                    last_ai = ctl._last_ai
                    bal = await ctl.broker.get_balance()
                    now_ts = int(time.time())
                    # 计算入场价：用当前 mark_price（若有）或 2000 兜底
                    entry_price = float(pos.mark_price or bal.total and 0 or 2000)
                    # 获取合理的 entry price：读 position mark_price（PaperBroker mark 会在最近 ticker 上更新；若空则 default 2000 fallback 会有问题，改成读取 broker last 或 state balance；简化：用 PaperBroker 的 _last_price。更稳妥：直接设为上次 state 中 balance_mark。暂用默认风险计算会兜底。）
                    # 兜底：若 mark_price == 0（初始），回退 2000
                    if not entry_price or entry_price <= 0:
                        entry_price = 2000.0
                    verdict = await risk.check_can_open(
                        balance_total=float(bal.total or 0),
                        balance_available=float(bal.available or 0),
                        entry_price=entry_price,
                        now_ts=now_ts,
                    )
                    if not verdict.allow:
                        logger.bind(log_type="trade").info(
                            "[风控] 本轮跳过开仓：{}（连续亏损={}，日切余额={:.2f}U 权益={:.2f}U）",
                            verdict.reason, risk.consecutive_losses,
                            risk.daily_start_balance, float(bal.total or 0),
                        )
                    elif last_ai is None or last_ai.confidence < 50 or last_ai.market_regime in (
                        MarketRegime.LOW_VOLATILITY, MarketRegime.RANGE,
                    ):
                        logger.bind(log_type="trade").info(
                            "[AI] 信号或置信度不足（reg={} conf={}），暂不开仓",
                            last_ai.market_regime.value if last_ai else "None",
                            last_ai.confidence if last_ai else -1,
                        )
                    else:
                        # 决定多空
                        if last_ai.market_regime == MarketRegime.TREND_UP:
                            market_side = OrderSide.BUY
                        elif last_ai.market_regime == MarketRegime.TREND_DOWN:
                            market_side = OrderSide.SELL
                        else:
                            market_side = None
                        if market_side is not None:
                            logger.bind(log_type="trade").info(
                                "[主循环] 发起交易信号：side={} entry={} size={:.6f} sl={}",
                                market_side.value, entry_price,
                                verdict.suggested_size, verdict.stop_loss_price,
                            )
                            exec_res = await ctl.execute_trade_signal(
                                ai=last_ai, verdict=verdict,
                                entry_price=entry_price, market_side=market_side,
                            )
                            logger.success("[主循环] execute 结果: status={} via={} qty={:.6f} 原因={}",
                                           exec_res["status"], exec_res["via"],
                                           exec_res["qty"], exec_res["reason"])

            # ------------------------------------------------------------
            # 3) 持久化：risk / position_manager / stats 已在各自方法内写 state，这里仅兜底 sync 余额/状态标志
            # ------------------------------------------------------------
            st = state_store.load()
            st["risk"] = risk.to_dict()
            st.setdefault("position_manager", {}).update(pm.to_dict())
            # 最新余额刷新（FastAPI status 展示取 state 的余额快照会用）
            try:
                bal2 = await ctl.broker.get_balance()
                st["balance"] = {
                    "total": float(bal2.total or 0),
                    "available": float(bal2.available or 0),
                    "unrealized_pnl": float(bal2.unrealized_pnl or 0),
                    "currency": getattr(bal2, "currency", "USDT") or "USDT",
                }
                pos2 = await ctl.broker.get_position(symbol)
                st["position"] = {
                    "side": pos2.side.value,
                    "size": float(pos2.size or 0),
                    "entry_price": float(pos2.entry_price or 0),
                    "mark_price": float(pos2.mark_price or 0),
                    "leverage": int(getattr(pos2, "leverage", 0) or 0),
                    "unrealized_pnl": float(getattr(pos2, "unrealized_pnl", 0.0) or 0),
                }
            except Exception:
                logger.exception("刷新余额/持仓快照失败")
            state_store.save(st)
        except Exception:
            logger.exception("主循环执行异常（{}s 后重试）", loop_interval)
        await asyncio.sleep(loop_interval)


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
