"""对话领域对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ConversationTurnRecord:
    """单轮问答记录。"""

    id: int
    conversation_id: str
    user_text: str
    assistant_text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """单个会话及其问答轮次。"""

    conversation_id: str
    orchestration_id: str | None
    session_id: str
    machine_id: str
    started_at: datetime
    updated_at: datetime
    turns: tuple[ConversationTurnRecord, ...] = field(default_factory=tuple)


__all__ = [
    "ConversationRecord",
    "ConversationTurnRecord",
]
