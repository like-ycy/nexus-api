"""环境管理领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EnvironmentPointRecord:
    """单个环境点位。"""

    id: str
    environment_id: str
    name: str
    tag: str
    description: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EnvironmentRecord:
    """单个环境配置。"""

    id: str
    name: str
    description: str
    extra: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    points: tuple[EnvironmentPointRecord, ...] = ()


__all__ = ["EnvironmentPointRecord", "EnvironmentRecord"]
