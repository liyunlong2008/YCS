"""Broker 工厂：根据运行模式返回 PaperBroker 或 OKXBroker，shadow_mode=True 时再包一层 ShadowBroker。"""

from __future__ import annotations

from loguru import logger

from .base import Broker
from .shadow import ShadowBroker
from ..core.config import AppConfig, OKXConfig
from ..core.constants import RunMode


def build_broker(cfg: AppConfig) -> Broker:
    """根据 trading.live 构建 Broker 实例。

    A7 影子模式（risk_limits.shadow_mode=True）：
      · 无论 PAPER 还是 LIVE，都额外套一层 ShadowBroker，
        保证 write 路径（place/cancel）100% 不真发。
      · LIVE 下即使配置了真实 OKX 密钥也不会真发（影子模式的核心承诺）。
    """
    if cfg.trading.mode == RunMode.PAPER:
        # PaperBroker 不需要 OKX 密钥，但仍需要 OKX 拉行情，
        # 故先允许 OKX 配置为空占位。
        from .paper import PaperBroker
        inner: Broker = PaperBroker(symbol=cfg.trading.symbol)
    else:
        shadow = bool(getattr(cfg.risk_limits, "shadow_mode", False))
        # 影子模式：即使 OKX 是占位值也允许 new OKXBroker（ShadowBroker 会拦截写路径，
        # 不会真调私有 API）。但如果用户填了真实密钥，就直接用——读路径（行情/余额/挂单）
        # 照样走真实 OKX，保证观察数据真实。
        okx_cfg = cfg.okx
        creds_ok = bool(okx_cfg.api_key and okx_cfg.secret and okx_cfg.passphrase)
        if not shadow and not creds_ok:
            raise AssertionError("实盘模式必须填写 config.yaml 中 okx.* 三项（影子模式除外）")

        from .okx_broker import OKXBroker
        # 即使占位缺值也 OKXBroker：后续读接口（get_balance / get_position 等）可能抛错，
        # 但影子模式下 Dashboard 会显示友好的"未登录 OKX"提示，不会误下真单。
        # 用空字符串兜底，确保构造不崩。
        if not creds_ok:
            okx_cfg = OKXConfig(
                api_key=okx_cfg.api_key or "",
                secret=okx_cfg.secret or "",
                passphrase=okx_cfg.passphrase or "",
            )
        inner = OKXBroker(symbol=cfg.trading.symbol, okx=okx_cfg)

    shadow_final = bool(getattr(cfg.risk_limits, "shadow_mode", False))
    if shadow_final:
        logger.info("[build_broker] shadow_mode=True → 使用 ShadowBroker 包装 inner={}",
                    type(inner).__name__)
        return ShadowBroker(inner, symbol=cfg.trading.symbol)
    return inner
