"""对话记录业务服务。"""

from __future__ import annotations

import asyncio
import contextlib

from dataclasses import dataclass
from typing import Any

from src.db.conversation_store import ConversationStore, PostgresConversationStore
from src.domain import ConversationRecord
from src.domain.session import SessionInfo
from src.utils.logging import logger


@dataclass(frozen=True, slots=True)
class _TurnRecordedEvent:
    conversation_id: str
    orchestration_id: str | None
    session_id: str
    machine_id: str
    user_text: str
    assistant_text: str


@dataclass(frozen=True, slots=True)
class SessionListResult:
    total: int
    sessions: tuple[ConversationRecord, ...]


@dataclass(frozen=True, slots=True)
class SessionDetailResult:
    total: int
    session: ConversationRecord | None


class ConversationService:
    """负责对话记录的写入调度与查询。"""

    def __init__(
        self,
        store: ConversationStore,
        *,
        queue_size: int = 1000,
    ) -> None:
        self._store = store
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=queue_size)
        self._worker_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._worker_task is not None:
                return
            await self._store.open()
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="nexus-conversation-writer",
            )

    async def close(self) -> None:
        async with self._start_lock:
            if self._worker_task is None:
                await self._store.close()
                return
        await self.wait_until_idle()
        worker = self._worker_task
        self._worker_task = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await self._store.close()

    async def record_turn(
        self,
        session: SessionInfo,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        await self.start()
        self._enqueue(
            _TurnRecordedEvent(
                conversation_id=session.session_id,
                orchestration_id=session.orchestration_id,
                session_id=session.session_id,
                machine_id=session.machine_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        )

    async def list_sessions(
        self,
        *,
        orchestration_id: str,
        limit: int,
        offset: int,
    ) -> SessionListResult:
        await self.start()
        await self.wait_until_idle()
        total = await self._store.count_conversations(
            orchestration_id=orchestration_id,
            session_id=None,
        )
        sessions = await self._store.list_conversations(
            orchestration_id=orchestration_id,
            session_id=None,
            limit=limit,
            offset=offset,
            turn_limit=1,
            turn_offset=0,
        )
        return SessionListResult(total=total, sessions=tuple(sessions))

    async def get_session(
        self,
        *,
        orchestration_id: str,
        session_id: str,
        limit: int,
        offset: int,
    ) -> SessionDetailResult:
        await self.start()
        await self.wait_until_idle()
        total = await self._store.count_turns(
            orchestration_id=orchestration_id,
            session_id=session_id,
        )
        sessions = await self._store.list_conversations(
            orchestration_id=orchestration_id,
            session_id=session_id,
            limit=1,
            offset=0,
            turn_limit=limit,
            turn_offset=offset,
        )
        return SessionDetailResult(
            total=total,
            session=sessions[0] if sessions else None,
        )

    async def wait_until_idle(self) -> None:
        await self._queue.join()

    async def _worker_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._handle_event(event)
            except Exception as exc:
                logger.error("写入对话记录失败: {}", exc)
            finally:
                self._queue.task_done()

    async def _handle_event(self, event: object) -> None:
        if isinstance(event, _TurnRecordedEvent):
            await self._store.append_turn(
                conversation_id=event.conversation_id,
                orchestration_id=event.orchestration_id,
                session_id=event.session_id,
                machine_id=event.machine_id,
                user_text=event.user_text,
                assistant_text=event.assistant_text,
            )
            return

        raise TypeError(f"未知的对话事件类型: {type(event)!r}")

    def _enqueue(self, event: object) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("对话记录队列已满，已丢弃事件: {}", type(event).__name__)


def build_conversation_service(config: dict[str, Any]) -> ConversationService | None:
    """根据配置创建对话记录服务。"""
    database_config = _require_section(config, "database")
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None

    conversation_config = _require_section(config, "conversation")

    store = PostgresConversationStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )
    return ConversationService(
        store,
        queue_size=int(conversation_config.get("queue_size") or 1000),
    )


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


__all__ = [
    "ConversationService",
    "SessionDetailResult",
    "SessionListResult",
    "build_conversation_service",
]
