"""
启动前安全自检：
  - 实盘(live=true)：OKX / AI 密钥为空或占位 → raise RuntimeError，阻止启动
  - 纸盘(live=false)：占位值仅警告，不阻止启动
占位值定义：
  1) None / 空串 / 仅空白
  2) 以 "YOUR_" 开头的示例占位（不区分大小写）
  3) 常见占位："xxx", "TODO", "changeme", "example", "test", "placeholder"（小写比较）
"""
from __future__ import annotations

import re
from loguru import logger


# 占位特征：命中任一即视为占位
_PLACEHOLDER_PREFIXES = ("your_",)
_PLACEHOLDER_EXACT = {
    "xxx", "todo", "changeme", "change_me", "example",
    "test", "placeholder", "demo", "none", "null", "",
}
_PLACEHOLDER_RE = re.compile(r"^\s*(your[-_]|changeme|todo[\s_-]*$|xxxx+$)", re.IGNORECASE)


def _is_placeholder(value: object) -> bool:
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    low = s.lower()
    if low in _PLACEHOLDER_EXACT:
        return True
    for prefix in _PLACEHOLDER_PREFIXES:
        if low.startswith(prefix):
            return True
    if _PLACEHOLDER_RE.match(s):
        return True
    # 连续 5 个以上的 X / *（常作遮罩占位）
    if re.fullmatch(r"[xX\*]{5,}", s):
        return True
    return False


def validate_runtime_credentials(
    *,
    live: bool,
    okx_api_key: str,
    okx_secret: str,
    okx_passphrase: str,
    ai_api_key: str,
) -> bool:
    """
    Returns True 表示校验通过；live=True 不通过时 raise RuntimeError。
    纸盘模式仅打印 WARNING 级日志。
    """
    problems: list[str] = []

    checks = [
        ("okx.api_key",       okx_api_key),
        ("okx.secret",        okx_secret),
        ("okx.passphrase",    okx_passphrase),
        ("ai.api_key",        ai_api_key),
    ]

    # 纸盘模式下 OKX 占位允许（因为用 PaperBroker），但 AI 占位仍提示
    paper_only_warnings: list[str] = []

    for name, value in checks:
        if _is_placeholder(value):
            if not live and name.startswith("okx."):
                paper_only_warnings.append(f"{name}=占位值（纸盘模式跳过）")
                continue
            problems.append(name)

    if not live:
        if paper_only_warnings:
            for item in paper_only_warnings:
                logger.warning("[安全自检·纸盘] {}", item)
        if _is_placeholder(ai_api_key):
            logger.warning("[安全自检·纸盘] ai.api_key=占位值：AI 将使用回退默认行情(震荡/中性)")
        return True

    # 实盘：任一占位即拒绝启动
    if problems:
        raise RuntimeError(
            "实盘模式启动被安全自检拦截：以下密钥仍是占位值，请填入真实凭证后再启动。\n"
            "  · 问题字段: " + ", ".join(problems) + "\n"
            "  · 纸盘/仿真模式请修改 config.yaml → trading.live: false\n"
            "  · 占位特征: 空值 / YOUR_开头 / xxx / TODO / changeme / placeholder 等"
        )
    return True
