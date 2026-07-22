"""云端 session 数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """单个会话的权威信息。"""

    session_id: str
    machine_id: str
    orchestration_id: str | None = None


class ConversationRecorder(Protocol):
    """记录问答轮次的抽象接口。"""

    async def record_turn(
        self,
        session: SessionInfo,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None: ...
