"""机器人技能领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RobotSkillRecord:
    """单个机器人当前注册的一条技能定义。"""

    robot_id: str
    skill_id: str
    skill_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    supports_cancel: bool = False


__all__ = ["RobotSkillRecord"]
