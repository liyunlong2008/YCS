# -*- coding: utf-8 -*-
"""Broker 工厂：根据运行模式返回 PaperBroker 或 OKXBroker。"""

from __future__ import annotations

from .base import Broker
from ..core.config import AppConfig, OKXConfig
from ..core.constants import RunMode


def build_broker(cfg: AppConfig) -> Broker:
    """根据 trading.live 构建 Broker 实例。"""
    if cfg.trading.mode == RunMode.PAPER:
        # PaperBroker 不需要 OKX 密钥，但仍需要 OKX 拉行情，
        # 故先允许 OKX 配置为空占位。
        from .paper import PaperBroker
        return PaperBroker(symbol=cfg.trading.symbol)

    # 实盘模式：必须校验 OKX 凭证
    okx_cfg = cfg.okx
    assert okx_cfg.api_key and okx_cfg.secret and okx_cfg.passphrase, \
        "实盘模式必须填写 config.yaml 中 okx.* 三项"

    from .okx_broker import OKXBroker
    return OKXBroker(symbol=cfg.trading.symbol, okx=okx_cfg)
