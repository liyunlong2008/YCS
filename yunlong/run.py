# =============================================================================
# 云龙挑战赛（YCS）启动入口
# 用法：
#   cd yunlong
#   ../.venv/bin/python run.py          # 或  uv run python yunlong/run.py
# =============================================================================

import sys
from pathlib import Path

# 确保项目根目录可导入
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from loguru import logger


def setup_logger() -> None:
    """初始化 Loguru 日志：system / trade / error 三类文件。"""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 清除默认 handler，避免重复输出
    logger.remove()

    # 控制台输出（INFO 级别以上）
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level:<7}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        enqueue=True,
    )

    # system.log：系统全量日志
    logger.add(
        log_dir / "system.log",
        level="DEBUG",
        rotation="00:00",       # 每天轮转
        retention="30 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )

    # trade.log：仅交易相关事件
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

    # error.log：仅错误级别
    logger.add(
        log_dir / "error.log",
        level="ERROR",
        rotation="00:00",
        retention="60 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )


def main() -> None:
    """云龙挑战赛系统主入口。"""
    setup_logger()
    logger.info("=" * 60)
    logger.info("云龙挑战赛系统（YCS）启动中...")
    logger.info("项目目录: {}", PROJECT_ROOT)

    # 延迟导入，保证 logger 先初始化
    from app.api.app import create_app

    app = create_app()

    logger.info("启动 FastAPI Dashboard (http://127.0.0.1:8000)")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
