# =============================================================================
# 云龙挑战赛（YCS）系统启动入口（完整装配版）
# 用法：
#   cd /workspace
#   .venv/bin/python run.py          # 默认：加载 config.yaml，监听 <config.server.host 即 0.0.0.0>:<port 8765>
#   .venv/bin/python run.py --dev    # 开发模式：绑定 127.0.0.1:8765（公网不通，仅本机）
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

    # === 注入 state_store 前的小优化：AI 密钥占位时，标记到 cfg 方便 FastAPI /api/ai/analyze 直接跳过联网 ===
    from app.core.safety import _is_placeholder as __ai_ph
    __PLACHOLDER_AI = __ai_ph(cfg.ai.api_key)
    # 用 AppConfig 动态属性暂存（FastAPI 侧优先读 runtime['config'].ai._placeholder_api_key）
    try:
        object.__setattr__(cfg.ai, "_placeholder_api_key", bool(__PLACHOLDER_AI))
    except Exception:
        pass  # Pydantic 拒绝时不影响主流程

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

    # 3.1) MarketSpec：交易所最小下单 / 面值 / 杠杆上限（实盘从 OKX 拉；失败 fallback 默认值，绝不影响启动）
    from app.broker.base import MarketSpec
    try:
        market_spec: MarketSpec = await broker.fetch_market_spec(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉取交易所 MarketSpec 失败（将使用 ETH-USDT-SWAP 默认保守值）：{}", exc)
        market_spec = MarketSpec(symbol=symbol)
    logger.info("MarketSpec: source={} ctVal={} minSz={} lotSz={} szDecimals={} maxLever={} minNotionalUsdt={}",
                market_spec.source, market_spec.ct_val, market_spec.min_sz, market_spec.lot_sz,
                market_spec.sz_decimals, market_spec.max_lever, market_spec.min_notional_usdt or "(按 minSz*ctVal*entry 推算)")

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
        # 2026-08-30 升级：不再只做简单的 bal>0 && daily_start<=0 判断（会让 state 残留 1000U 绕过检查）。
        #   ① 先从 state 读 daily_reset_day 与今天比较，判断是否跨天；
        #   ② 调用 risk.recompute_daily_start_if_suspicious() 三层纠偏：
        #      - 跨天 → 强制重置
        #      - 首次启动 daily_start=0 → 初始化
        #      - 残留大值 / 充值小值（差>50U 且倍数超阈值）→ 重置
        import datetime as _dt
        _today = _dt.date.today().isoformat()
        _st_pre = state_store.load()
        last_day = (_st_pre.get("daily_reset_day") or "")
        day_match = bool(last_day) and last_day == _today
        reset, reason = risk.recompute_daily_start_if_suspicious(
            bal_total,
            daily_reset_day_matches=day_match,
            today_iso=_today,
        )
        if reset:
            logger.success("[日切初始化] {}", reason)
        else:
            logger.info("[日切初始化] {}", reason)
        _st_pre["daily_reset_day"] = _today
        _st_pre.setdefault("risk", {}).update(risk.to_dict())
        _st_pre.setdefault("position_manager", {}).update(position_manager.to_dict())
        state_store.save(_st_pre)
    except Exception:
        logger.exception("启动恢复失败，以 STOPPED 继续启动 Dashboard（请检查 OKX 网络/密钥）")

    # 6.5) 兜底写 started_at：无论 recoverer 是否异常路径，都保证 state_store.started_at 为 int epoch 秒
    #      · 若已有合法 int started_at（恢复流程已写入或上次进程残留）→ 不覆盖
    #      · 若 started_at 为 None / 非法字符串 / 0 → 写当前时间
    import datetime as _dtb, time as _tb
    st_snap = state_store.load()
    raw_ts = st_snap.get("started_at")
    if isinstance(raw_ts, str):
        try:
            st_snap["started_at"] = int(_dtb.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:  # noqa: BLE001
            st_snap["started_at"] = int(_tb.time())
    elif not isinstance(raw_ts, int) or raw_ts <= 0:
        st_snap["started_at"] = int(_tb.time())
    state_store.save(st_snap)

    # 7) 注入 FastAPI runtime
    app.state.runtime.update({
        "config": cfg,
        "broker": broker,
        "ai": ai,
        "risk": risk,
        "position_manager": position_manager,
        "storage": (state_store, journal),
        "market_producer": market_producer,
        "market_spec": market_spec,        # 2026-08-30：新增，risk.check_can_open 会消费（USDT 口径判单）
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
    """交易主循环（2026-08-30 改版：自适应 AI 节流 7 级状态机 + 价格哨兵）。

    核心改动（解决 VPS 现场『32 分钟 22 次 AI / 失败 4 次也瞎调 / 熔断期也浪费』三痛）：
      · 轮询间隔从 30s 缩到 10s：价格哨兵更灵敏（≥1% 波动 10s 内就能早叫 AI）
      · 每轮先 ctl.should_analyze()：返回 should_call=True 才真正 ctl.analyze()
      · 未到时（冷却中/STOP/熔断/DEGRADED）：完全不调用 AI，只本地价格哨兵 → 日均 960 次砍到 ≤150 次
      · early_wake（1m 波动≥1%/大波动≥2%）：强制调 AI，熔断/睡眠窗也不漏行情
    """
    from app.core.constants import SystemStatus, OrderSide, MarketRegime, PositionSide
    loop_interval = 10  # 10s 轮询（价格哨兵灵敏档；真正调 AI 仍按 7 级节奏）
    last_snapshot_bal_ts = 0  # 余额刷新节流：每轮不必全刷，30s 刷新一次即可（避免空转浪费）
    while True:
        try:
            state_store: StateStore = rt["state_store"]
            risk: RiskEngine = rt["risk"]
            pm: PositionManager = rt["position_manager"]
            ctl: TradingController = rt["controller"]
            cfg = rt["config"]
            symbol = cfg.trading.symbol
            st = state_store.load()
            now_ts = int(time.time())

            # ------------------------------------------------------------
            # 1) 日切点
            # ------------------------------------------------------------
            try:
                ctl.apply_daily_reset_if_needed()
            except Exception:
                logger.exception("日切点检测异常")

            # ------------------------------------------------------------
            # 2) 拉持仓 & 利润保护轮询
            # ------------------------------------------------------------
            pos = await ctl.broker.get_position(symbol)
            has_pos = pos.side != PositionSide.FLAT
            mark_price = float(getattr(pos, "mark_price", 0.0) or 0.0)
            if mark_price <= 0:
                mark_price = float((st.get("position") or {}).get("mark_price", 0.0) or 0.0)
            # 2026-08-31：空仓时 pos.mark_price / state_store.position.mark_price 常为 0，
            #   → 旧代码兜底 2466 写死 → 最小名义卡死 2.466U 不跟随现价（用户投诉点）。
            #   现在先独立调 broker.get_ticker_price() 拉最新价，再做最后的兜底（兜底打 WARNING）。
            if mark_price <= 0:
                try:
                    if hasattr(ctl.broker, "get_ticker_price") and callable(getattr(ctl.broker, "get_ticker_price")):
                        _tp = await ctl.broker.get_ticker_price(symbol)
                        if float(_tp or 0.0) > 0:
                            mark_price = float(_tp)
                except Exception:  # noqa: BLE001
                    pass
            if mark_price <= 0:
                mark_price = 2466.0
                logger.bind(log_type="trade").warning(
                    "[MARK_PRICE] 拿不到真实现价，已兜底=2466（最小开仓名义=2.466U，注意不是实时！）"
                    "请检查 broker ticker/position 接口能否正常拉取 ETH 现价")
            entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
            liq_price = float(getattr(pos, "liquidation_price", 0.0) or 0.0)

            # RUNNING 状态下做持仓利润保护（不控制空仓时还会额外扫）
            if st.get("status") == SystemStatus.RUNNING.value:
                need_close, close_reason = pm.should_close_for_protection(pos)
                if has_pos and need_close:
                    logger.bind(log_type="trade").warning("利润保护触发（主循环）：{}", close_reason)
                    realized = await ctl.close_position_for_protection(pos)
                    logger.success("[主循环] 利润保护平仓完成：已实现盈亏={:.6f}U", realized)
                    # 平仓后重新刷新 pos（下次进入空仓分支）
                    pos = await ctl.broker.get_position(symbol)
                    has_pos = pos.side != PositionSide.FLAT

            # ------------------------------------------------------------
            # 3) 【2026-08-30 新】AI 自适应节流决策（should_analyze 纯本地 O(1)，不调 AI）
            #    即便系统≠RUNNING / 风控熔断 / 睡眠窗，也照样跑价格哨兵
            # ------------------------------------------------------------
            dec = await ctl.should_analyze(
                mark_price=mark_price,
                entry_price=entry_price,
                liquidation_price=liq_price,
                has_position=has_pos,
            )
            throttle_tag = f"[{dec.level.value}{' early_wake' if dec.early_wake else ''}]"

            if not dec.should_call:
                # —— 真·降频：本轮不调 AI，只打一条 TRACE 级节流日志（INFO 级别下不刷屏，只保留当日累计指标到 state_store）
                wait_s = max(dec.next_call_at - now_ts, 0)
                logger.opt(depth=0).debug(
                    "{} 跳过本轮 AI 调用（剩 {}s；原因：{}；波动={:.2f}%）",
                    throttle_tag, wait_s, dec.reason, dec.event_pct,
                )
            else:
                # ===== 真正调 AI：force=dec.early_wake 以标记 early_wake 模式 =====
                try:
                    await ctl.analyze(force=dec.early_wake)
                except Exception:
                    logger.exception("AI 分析异常（跳过本轮开仓）")

            # ------------------------------------------------------------
            # 4) 空仓 + RUNNING + allow_trading → 风控 → 下单
            #    （AI 没调过时用 ctl._last_ai 上一次结果，仍然合规）
            # ------------------------------------------------------------
            if (not has_pos) and st.get("status") == SystemStatus.RUNNING.value:
                last_ai = ctl._last_ai
                bal = await ctl.broker.get_balance()
                # entry_price 计算（沿用原有优先级：mark_price→state→market_producer→兜底）
                e_price = mark_price
                if e_price <= 0:
                    e_price = float((st.get("position") or {}).get("mark_price", 0.0) or 0.0)
                if e_price <= 0:
                    mp = getattr(ctl.market_producer, "last_mark_price", None)
                    if callable(mp):
                        try:
                            v = mp()
                            if isinstance(v, (int, float)) and v > 0:
                                e_price = float(v)
                        except Exception:  # noqa: BLE001
                            pass
                if e_price <= 0:
                    e_price = 2466.0
                verdict = await risk.check_can_open(
                    balance_total=float(bal.total or 0),
                    balance_available=float(bal.available or 0),
                    entry_price=e_price,
                    now_ts=now_ts,
                    market_spec=rt.get("market_spec"),
                    risk_limits=getattr(cfg, "risk_limits", None),
                    trading_config=getattr(cfg, "trading", None),
                )
                if not verdict.allow:
                    extra = (
                        f"（名义={verdict.suggested_notional_usdt:.2f}U / 最小={verdict.effective_min_notional_usdt:.2f}U "
                        f"/ 杠杆={verdict.suggested_leverage}X）"
                        if verdict.effective_min_notional_usdt > 0 else f"（杠杆={verdict.suggested_leverage}X）"
                    )
                    logger.bind(log_type="trade").info(
                        "[风控] 本轮跳过开仓：{}{}（连续亏损={}，日切余额={:.2f}U 权益={:.2f}U）",
                        verdict.reason, extra, risk.consecutive_losses,
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
                    if last_ai.market_regime == MarketRegime.TREND_UP:
                        market_side = OrderSide.BUY
                    elif last_ai.market_regime == MarketRegime.TREND_DOWN:
                        market_side = OrderSide.SELL
                    else:
                        market_side = None
                    if market_side is not None:
                        # 2026-08-30：双过(风控+AI信号)后打一次时间戳快照，供 Dashboard「最近交易信号就绪时间」
                        # 若 14:00 启动但这里始终=0 → 说明风控或 AI 之一没到位
                        setattr(risk, "last_pass_trade_signal_at",
                                int(getattr(risk, "last_pass_trade_signal_at", 0) or 0))
                        risk.last_pass_trade_signal_at = int(now_ts)
                        logger.bind(log_type="trade").info(
                            "[主循环] 发起交易信号：side={} entry={} sz={} 名义={:.2f}U sl={} lev={}X "
                            "(min_notional={:.2f}U, max_notional={:.2f}U) {}",
                            market_side.value, e_price,
                            f"{verdict.suggested_size:.4f}".rstrip("0").rstrip("."),
                            verdict.suggested_notional_usdt, verdict.stop_loss_price,
                            verdict.suggested_leverage,
                            verdict.effective_min_notional_usdt, verdict.effective_max_notional_usdt,
                            throttle_tag,
                        )
                        exec_res = await ctl.execute_trade_signal(
                            ai=last_ai, verdict=verdict,
                            entry_price=e_price, market_side=market_side,
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
# 默认端口权威值：AppConfig.server.port（ServerConfig 默认 8765）。
# 默认 host 权威值：AppConfig.server.host（ServerConfig 默认 0.0.0.0）。
# 解析 config.yaml 失败时，回退到下方 _FALLBACK_PORT / _FALLBACK_HOST。
_FALLBACK_PORT = 8765
_FALLBACK_HOST = "0.0.0.0"
_DEV_HOST = "127.0.0.1"   # --dev 模式固定回环（仅本机可访问，符合「开发模式」语义）


def _read_default_port_from_config() -> int:
    """尝试从项目根 config.yaml 读取 server.port；缺失/解析失败都返回兜底端口。"""
    try:
        from app.core.config import default_config_path, load_config  # noqa: PLC0415
        cfg_path = default_config_path()
        if cfg_path.is_file():
            cfg = load_config(cfg_path)
            port = int(getattr(cfg.server, "port", _FALLBACK_PORT))
            if 1 <= port <= 65535:
                return port
    except Exception:
        # 没 config.yaml / YAML 格式错误等：都静默回退，不阻塞启动
        pass
    return _FALLBACK_PORT


def _read_default_host_from_config() -> str:
    """尝试从项目根 config.yaml 读取 server.host；缺失/非法值都回退到 0.0.0.0（VPS 默认公网可达）。"""
    try:
        from app.core.config import default_config_path, load_config  # noqa: PLC0415
        cfg_path = default_config_path()
        if cfg_path.is_file():
            cfg = load_config(cfg_path)
            host = str(getattr(cfg.server, "host", _FALLBACK_HOST) or _FALLBACK_HOST).strip()
            if host:
                return host
    except Exception:
        pass
    return _FALLBACK_HOST


_DEFAULT_PORT = _read_default_port_from_config()
_DEFAULT_HOST = _read_default_host_from_config()


def main() -> None:
    parser = argparse.ArgumentParser(description="云龙挑战赛（YCS）自动交易系统启动器")
    parser.add_argument(
        "--dev",
        action="store_true",
        help=f"开发模式：仅本机回环，监听 {_DEV_HOST}:{_DEFAULT_PORT}",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"自定义监听 host；未传时取 config.server.host（默认 {_DEFAULT_HOST}）；--dev 则强制={_DEV_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"自定义监听 port；未传则取 config.server.port（默认 {_DEFAULT_PORT}）",
    )
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

    # 最终监听地址优先级：
    #   host: args.host（最高）→ --dev 强制 127.0.0.1 → config.server.host（默认 0.0.0.0）
    #   port: args.port（最高）→ config.server.port（默认 8765）
    if args.host:
        host = args.host
    elif args.dev:
        host = _DEV_HOST
    else:
        host = _DEFAULT_HOST
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
