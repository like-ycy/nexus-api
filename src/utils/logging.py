"""日志初始化工具。"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{name}:{function}:{line} - "
    "{message}"
)


def setup_logger(level: str = "INFO", log_path: Path | None = None) -> None:
    """根据配置初始化全局日志器。"""
    logger.remove()
    if log_path is not None:
        log_path.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_path / "nexus-api-{time:YYYY-MM-DD}.log",
            format=_FILE_FORMAT,
            level=level,
            rotation="00:00",
            retention="30 days",
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
            enqueue=True,
        )
    else:
        logger.add(
            sys.stderr,
            format=_CONSOLE_FORMAT,
            level=level,
            colorize=True,
            backtrace=True,
            diagnose=True,
            enqueue=True,
        )
