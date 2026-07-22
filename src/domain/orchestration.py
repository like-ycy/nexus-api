"""编排应用领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OrchestrationRecord:
    """单个编排应用配置。"""

    id: str
    name: str
    description: str
    prompt: str
    robot_id: str | None
    knowledge_base_id: str | None
    environment_id: str | None
    voice_id: str | None
    skill_ids: tuple[str, ...]
    welcome_message: str
    created_at: datetime
    updated_at: datetime


__all__ = ["OrchestrationRecord"]
