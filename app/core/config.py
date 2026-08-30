# -*- coding: utf-8 -*-
"""YAML 配置加载（设计文档 · 第七节）。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field

from .constants import RunMode, SYMBOL


class OKXConfig(BaseModel):
    """OKX API 凭证。"""
    api_key: str
    secret: str
    passphrase: str


class AIConfig(BaseModel):
    """AI 提供商配置（LiteLLM 统一接入）。"""
    provider: Literal["deepseek", "openai", "claude", "gemini", "openrouter"]
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = ""


class TradingConfig(BaseModel):
    """交易运行模式配置。"""
    live: bool = False
    symbol: str = SYMBOL
    # 2026-08-30：新增默认杠杆。小账户开仓的关键杠杆倍数：
    #   可开名义 ≈ 余额 × default_leverage；若交易所最小名义 = 2.5U（ETH-USDT-SWAP 约 0.1 张），
    #   14.83U 账户至少 lev≈2 才能勉强摸到最小；lev=5~10 才能让 risk_pct 模型稳定过 0.1 张。
    default_leverage: int = 10

    @property
    def mode(self) -> RunMode:
        return RunMode.LIVE if self.live else RunMode.PAPER


class RiskLimits(BaseModel):
    """实盘硬风控阈值（用户 2026-08-29：直接上实盘前按护栏方案补全，适配 14.8 USDT 超小账户）。

    护栏映射：
      A1. 本金上限硬锁 → live_max_equity_usdt
      A2. 每日亏损熔断（USDT 绝对值）→ live_max_daily_loss_usdt
      A3. 订单双因子 sanity → live_max_single_order_usdt + position_change_pct
           与 USDT 口径约束：min_order_notional_usdt / max_order_notional_usdt
      A5. Kill-Switch → kill_switch_token
      A7. Shadow 影子模式 → shadow_mode

    新增 2026-08-30：R 模型参数化（之前 RiskEngine 是类常量写死，14.8U 小账户会算 0.002 张被 minSz 拦下）：
      risk_per_trade_pct ：每笔最大允许亏 = total * r%（R 模型）
      stop_loss_price_pct：止损价相对入场价的价格百分比（无杠杆）
      min_order_notional_usdt：下单名义硬下限（USDT）；> 交易所 minNotional 会用更严格值
      max_order_notional_usdt：下单名义硬上限（USDT）；< 交易所 maxNotional 用更严格值
    """
    live_max_equity_usdt: float = 15.0
    live_max_daily_loss_usdt: float = 3.0
    live_max_single_order_usdt: float = 2.0
    position_change_pct: float = 0.10
    kill_switch_token: str = "YCS_KILL_CHANGEME_32BYTES_RANDOM_STRING_PLEASE"
    kill_panic_flatten: bool = True
    kill_http_timeout_s: int = 3
    emergency_halt_file: str = "data/EMERGENCY_HALT"
    shadow_mode: bool = False

    # R 模型（可配置）
    # 2026-08-30 校准：以 14.83U / ETH≈2466$ / 10X 杠杆 / 最小名义≈2.47U（sz≥0.1）为参照，
    #   math: sz_by_risk = total × R% / (entry*SL%*ctVal) / leverage
    #         0.1  ≤  14.83*R% / (2466*2.5%*0.01) / 10
    #      → R% ≥ 3.33%（留余量取 5%；嫌激进可调回 3.5%/2.5%）
    #   risk=5% + stop=2.5% + lev=10X → 单笔最大损=0.7415U；每张止损=2466*2.5%*0.01=0.6165U；
    #   sz=0.7415 / (0.6165*10) ≈ 0.12 张 → floor 到 0.1 张，名义 2.47U，满足最小下单。
    risk_per_trade_pct: float = 5.0
    stop_loss_price_pct: float = 2.5
    # USDT 名义上下限（最终生效值 = max(交易所 minNotional, config.min_order_notional_usdt) / min(交易所 max, config.max)）
    # 0 表示完全以交易所返回为准；非 0 会叠加更严格约束
    min_order_notional_usdt: float = 0.0
    max_order_notional_usdt: float = 0.0


class ServerConfig(BaseModel):
    """Dashboard / API 端口与监听配置。"""
    host: str = "0.0.0.0"   # 2026-08-30：默认从 127.0.0.1 改为 0.0.0.0（VPS 公网浏览器可直达）
    port: int = 8765      # 2026-08-30：默认从 8000 统一改为 8765
    ui_port: int = 8080


class LoggingConfig(BaseModel):
    """日志配置（Pydantic 校验，避免缺失字段时 fallback 散落在各处）。"""
    level: str = "INFO"
    file: str = "logs/app.log"


class StorageConfig(BaseModel):
    """交易记录存储路径配置。"""
    journal_dir: str = "data/journal"
    ledger_file: str = "data/ledger.jsonl"


class AppConfig(BaseModel):
    """应用根配置对象。"""
    okx: OKXConfig
    ai: AIConfig
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


def default_config_path() -> Path:
    """返回默认 config.yaml 绝对路径（允许 $CONFIG_PATH 环境变量覆盖）。

    优先级：
      1. 显式参数 > 2. $CONFIG_PATH > 3. <项目根>/config.yaml
    """
    env_v = os.environ.get("CONFIG_PATH")
    if env_v:
        return Path(env_v).resolve()
    return (Path(__file__).resolve().parent.parent.parent / "config.yaml").resolve()


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """从 YAML 文件加载并校验配置。

    Args:
        config_path: config.yaml 路径。None 时按 default_config_path() 规则解析。

    Returns:
        校验后的 AppConfig 实例。

    Raises:
        FileNotFoundError: 配置文件不存在。
        yaml.YAMLError: 包含 ParserError：YAML 语法非法；异常消息会附上「文件：具体行：上下文 ±3 行」。
        ValidationError: 配置字段缺失或非法。
    """
    path = Path(config_path) if config_path else default_config_path()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    text: str
    lines: list[str]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise OSError(f"读取配置失败: {path}: {e}") from e
    lines = text.splitlines()

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        # === 解析失败时把「文件 + 行号 + 上下文 ±3 行」塞进异常 message，避免 install.sh / pytest
        #     只看到 "while parsing a block mapping" 不知道哪一行写错（VPS 手改缩进错误最常见）。
        #
        # 注意：PyYAML 的 ParserError / ScannerError 都继承 MarkedYAMLError，其 __str__() 会
        # 根据 `.problem / .problem_mark / .context / .context_mark` 四个属性**重新拼 message**，
        # 所以 `type(e)(msg)` 这种「只传一个字符串」的方式会被 __init__ 当成 problem=None，导致
        # 自定义 header 完全丢失。必须用 `type(e)(problem, problem_mark, context, context_mark)`
        # 的标准四参形式构造新异常，custom_problem 里再放我们的中文 header + 原 problem。
        problem: str | None = getattr(e, "problem", None)
        context: str | None = getattr(e, "context", None)
        problem_mark: object | None = getattr(e, "problem_mark", None)
        context_mark: object | None = getattr(e, "context_mark", None)
        line_no = getattr(problem_mark, "line", None)
        col_no = getattr(problem_mark, "column", None)
        header_prefix = f"【YCS 配置语法错】文件 {path}"

        if isinstance(line_no, int) and line_no >= 0:
            # PyYAML line/col 均为 0-indexed → 展示用 1-indexed
            show_line = line_no + 1
            start = max(0, line_no - 3)
            end = min(len(lines), line_no + 4)
            context_lines: list[str] = []
            for idx in range(start, end):
                ln = idx + 1
                marker = ">>>" if ln == show_line else "   "
                safe = lines[idx].replace("\t", "\\t    ")   # Tab 可视化（YAML 头号坑）
                context_lines.append(f"  {marker} L{ln:04d}: {safe}")
            col_tip = ""
            if isinstance(col_no, int):
                # 列指针对齐 `L0005: ` 前缀：前缀宽度 = 2 + 3 + 1 + 1 + 4 + 1 + 1 = 13
                pointer = " " * 13 + " " * col_no + "^"
                context_lines.append(pointer)
                col_tip = f" 列 {col_no + 1}"
            header = (
                f"{header_prefix}，第 {show_line} 行{col_tip}：\n"
                f"  最常见原因：缩进混用（2/4/5 空格或 Tab）、无引号字符串里出现『冒号+空格』、或 flow 映射 {{ }} 没闭合。\n"
                f"  上下文（>>>=报错行）：\n" + "\n".join(context_lines) + "\n"
                f"  原始报错："
            )
            # 关键：把 header 拼到 problem 上，再用 MarkedYAMLError 标准 4 参构造，保证 __str__
            # 最终输出包含中文行号提示，而不是 PyYAML 默认的 `<unicode string>` 模糊消息。
            custom_problem = header + (problem or "")
            new_exc = type(e)(custom_problem, problem_mark, context, context_mark)
            # 额外保留 note（少数 YAML 子类型会附加）
            note = getattr(e, "note", None)
            if note is not None:
                try:
                    setattr(new_exc, "note", note)
                except AttributeError:
                    pass
            raise new_exc from e
        else:
            # 极少数没有 problem_mark 的 YAMLError：退化为简单前缀拼接
            fallback_problem = f"{header_prefix}：{problem or e}"
            try:
                raise type(e)(fallback_problem, problem_mark, context, context_mark) from e
            except TypeError:
                # 非 MarkedYAMLError 兼容兜底（例如 yaml.reader.ReaderError 参数可能不同）
                raise type(e)(fallback_problem) from e

    return AppConfig.model_validate(raw)


def ensure_config_file(config_path: str | Path) -> Tuple[Path, bool]:
    """确保目标 config.yaml 存在；不存在时从同目录 config.yaml.example 复制一份。

    与 deploy/ycsctl._ensure_config_from_example 语义一致（单一事实来源是 .example 模板）。

    Args:
        config_path: 期望的 config.yaml 路径。

    Returns:
        (resolved_path, created)
        - created=True  本函数刚从 example 复制成功（首次初始化）
        - created=False 已存在（不覆盖用户真实密钥）
    """
    path = Path(config_path)
    if path.is_file():
        return path.resolve(), False
    example = path.with_name(path.name + ".example")
    if not example.is_file():
        # 兜底：项目根的 .example（调用方传了非标准文件名 / 子目录时生效）
        root_example = path.parent / "config.yaml.example"
        if root_example.is_file() and example.resolve() != root_example.resolve():
            example = root_example
        else:
            return path.resolve(), False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, path)
        # 含密钥文件：最小权限（600）
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return path.resolve(), False
    return path.resolve(), True
