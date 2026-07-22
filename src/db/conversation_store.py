"""对话会话存储抽象与 PostgreSQL 实现。"""

from __future__ import annotations

from typing import Any, Protocol

from src.domain import ConversationRecord, ConversationTurnRecord
from src.utils.logging import logger


class ConversationStore(Protocol):
    """对话持久化接口。"""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ensure_conversation(
        self,
        *,
        conversation_id: str,
        orchestration_id: str | None,
        session_id: str,
        machine_id: str,
    ) -> None: ...

    async def append_turn(
        self,
        *,
        conversation_id: str,
        orchestration_id: str | None,
        session_id: str,
        machine_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None: ...

    async def list_conversations(
        self,
        *,
        orchestration_id: str,
        session_id: str | None,
        limit: int,
        offset: int,
        turn_limit: int,
        turn_offset: int,
    ) -> list[ConversationRecord]: ...

    async def count_conversations(
        self,
        *,
        orchestration_id: str,
        session_id: str | None,
    ) -> int: ...

    async def count_turns(
        self,
        *,
        orchestration_id: str,
        session_id: str,
    ) -> int: ...


class PostgresConversationStore:
    """基于 PostgreSQL 的对话历史存储。"""

    def __init__(
        self,
        *,
        dsn: str,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        command_timeout_sec: float = 10.0,
    ) -> None:
        self._dsn = dsn.strip()
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._command_timeout_sec = command_timeout_sec
        self._pool: Any | None = None

    async def open(self) -> None:
        if self._pool is not None:
            return

        try:
            import asyncpg
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 asyncpg，无法启用 conversation PostgreSQL 存储。请先执行 uv sync。"
            ) from exc

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            command_timeout=self._command_timeout_sec,
        )
        await self._initialize_schema()

    async def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def ensure_conversation(
        self,
        *,
        conversation_id: str,
        orchestration_id: str | None,
        session_id: str,
        machine_id: str,
    ) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO conversation_tbl (
                id,
                orchestration_id,
                session_id,
                machine_id,
                started_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE
            SET
                orchestration_id = EXCLUDED.orchestration_id,
                session_id = EXCLUDED.session_id,
                machine_id = EXCLUDED.machine_id,
                updated_at = NOW()
            """,
            conversation_id,
            orchestration_id,
            session_id,
            machine_id,
        )

    async def append_turn(
        self,
        *,
        conversation_id: str,
        orchestration_id: str | None,
        session_id: str,
        machine_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        pool = self._require_pool()
        await self.ensure_conversation(
            conversation_id=conversation_id,
            orchestration_id=orchestration_id,
            session_id=session_id,
            machine_id=machine_id,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO conversation_turn_tbl (
                        conversation_id,
                        machine_id,
                        user_text,
                        assistant_text,
                        created_at
                    )
                    VALUES ($1, $2, $3, $4, NOW())
                    """,
                    conversation_id,
                    machine_id,
                    user_text,
                    assistant_text,
                )
                await conn.execute(
                    """
                    UPDATE conversation_tbl
                    SET updated_at = NOW()
                    WHERE id = $1
                    """,
                    conversation_id,
                )

    async def list_conversations(
        self,
        *,
        orchestration_id: str,
        session_id: str | None,
        limit: int,
        offset: int,
        turn_limit: int,
        turn_offset: int,
    ) -> list[ConversationRecord]:
        pool = self._require_pool()
        if session_id:
            conversation_rows = await pool.fetch(
                """
                SELECT
                    id,
                    orchestration_id,
                    session_id,
                    machine_id,
                    started_at,
                    updated_at
                FROM conversation_tbl
                WHERE orchestration_id = $1 AND session_id = $2
                ORDER BY updated_at DESC, started_at DESC
                LIMIT $3 OFFSET $4
                """,
                orchestration_id,
                session_id,
                limit,
                offset,
            )
        else:
            conversation_rows = await pool.fetch(
                """
                SELECT
                    id,
                    orchestration_id,
                    session_id,
                    machine_id,
                    started_at,
                    updated_at
                FROM conversation_tbl
                WHERE orchestration_id = $1
                ORDER BY updated_at DESC, started_at DESC
                LIMIT $2 OFFSET $3
                """,
                orchestration_id,
                limit,
                offset,
            )

        if not conversation_rows:
            return []

        conversation_ids = [str(row["id"]) for row in conversation_rows]
        turn_rows = await pool.fetch(
            """
            SELECT
                id,
                conversation_id,
                user_text,
                assistant_text,
                created_at
            FROM (
                SELECT
                    id,
                    conversation_id,
                    user_text,
                    assistant_text,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY conversation_id
                        ORDER BY id DESC
                    ) AS rank_no
                FROM conversation_turn_tbl
                WHERE conversation_id = ANY($1::TEXT[])
            ) ranked
            WHERE rank_no > $2 AND rank_no <= $3
            ORDER BY conversation_id ASC, id ASC
            """,
            conversation_ids,
            turn_offset,
            turn_offset + turn_limit,
        )

        turns_by_conversation: dict[str, list[ConversationTurnRecord]] = {
            current_conversation_id: [] for current_conversation_id in conversation_ids
        }
        for row in turn_rows:
            turns_by_conversation[str(row["conversation_id"])].append(
                ConversationTurnRecord(
                    id=int(row["id"]),
                    conversation_id=str(row["conversation_id"]),
                    user_text=str(row["user_text"] or ""),
                    assistant_text=str(row["assistant_text"] or ""),
                    created_at=row["created_at"],
                )
            )

        result: list[ConversationRecord] = []
        for row in conversation_rows:
            current_conversation_id = str(row["id"])
            result.append(
                ConversationRecord(
                    conversation_id=current_conversation_id,
                    orchestration_id=(
                        str(row["orchestration_id"])
                        if row["orchestration_id"] is not None
                        else None
                    ),
                    session_id=str(row["session_id"]),
                    machine_id=str(row["machine_id"]),
                    started_at=row["started_at"],
                    updated_at=row["updated_at"],
                    turns=tuple(turns_by_conversation.get(current_conversation_id, [])),
                )
            )
        return result

    async def count_conversations(
        self,
        *,
        orchestration_id: str,
        session_id: str | None,
    ) -> int:
        pool = self._require_pool()
        if session_id:
            return int(
                await pool.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM conversation_tbl
                    WHERE orchestration_id = $1 AND session_id = $2
                    """,
                    orchestration_id,
                    session_id,
                )
                or 0
            )
        return int(
            await pool.fetchval(
                """
                SELECT COUNT(*)
                FROM conversation_tbl
                WHERE orchestration_id = $1
                """,
                orchestration_id,
            )
            or 0
        )

    async def count_turns(
        self,
        *,
        orchestration_id: str,
        session_id: str,
    ) -> int:
        pool = self._require_pool()
        return int(
            await pool.fetchval(
                """
                SELECT COUNT(turns.id)
                FROM conversation_tbl conversations
                LEFT JOIN conversation_turn_tbl turns
                    ON turns.conversation_id = conversations.id
                WHERE conversations.orchestration_id = $1
                  AND conversations.session_id = $2
                """,
                orchestration_id,
                session_id,
            )
            or 0
        )

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS conversation_tbl (
                id TEXT PRIMARY KEY,
                orchestration_id TEXT NULL,
                session_id TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_tbl_orchestration_updated
            ON conversation_tbl (orchestration_id, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS conversation_turn_tbl (
                id BIGSERIAL PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversation_tbl (id)
                    ON DELETE CASCADE,
                machine_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_turn_tbl_conversation_created
            ON conversation_turn_tbl (conversation_id, id DESC)
            """,
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("对话历史 PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


__all__ = [
    "ConversationRecord",
    "ConversationStore",
    "ConversationTurnRecord",
    "PostgresConversationStore",
]
