"""项目内复用的核心枚举定义。"""

from __future__ import annotations

from enum import Enum


class DebugLevel(str, Enum):
    """日志级别枚举。"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


__all__ = ["DebugLevel"]
