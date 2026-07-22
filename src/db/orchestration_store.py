"""orchestration_tbl 存储抽象与 PostgreSQL 实现。"""

from __future__ import annotations

from typing import Any, Protocol

from src.domain import OrchestrationRecord
from src.utils.logging import logger


class OrchestrationStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def create_orchestration(
        self,
        *,
        orchestration_id: str,
        name: str,
        description: str,
        prompt: str,
        robot_id: str | None,
        knowledge_base_id: str | None,
        environment_id: str | None,
        voice_id: str | None,
        skill_ids: list[str],
        welcome_message: str,
    ) -> OrchestrationRecord: ...
    async def get_orchestration(
        self, orchestration_id: str
    ) -> OrchestrationRecord | None: ...
    async def count_orchestrations(self) -> int: ...
    async def list_orchestrations(
        self, *, limit: int, offset: int
    ) -> list[OrchestrationRecord]: ...
    async def update_orchestration(
        self,
        *,
        orchestration_id: str,
        name: str,
        description: str,
        prompt: str,
        robot_id: str | None,
        knowledge_base_id: str | None,
        environment_id: str | None,
        voice_id: str | None,
        skill_ids: list[str],
        welcome_message: str,
    ) -> OrchestrationRecord | None: ...
    async def delete_orchestration(self, orchestration_id: str) -> bool: ...


class PostgresOrchestrationStore:
    """基于 PostgreSQL 的 orchestration_tbl 存储。"""

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
                "未安装 asyncpg，无法启用 orchestration PostgreSQL 存储。请先执行 uv sync。"
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

    async def create_orchestration(
        self,
        *,
        orchestration_id: str,
        name: str,
        description: str,
        prompt: str,
        robot_id: str | None,
        knowledge_base_id: str | None,
        environment_id: str | None,
        voice_id: str | None,
        skill_ids: list[str],
        welcome_message: str,
    ) -> OrchestrationRecord:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO orchestration_tbl (
                id, name, description, prompt, robot_id, knowledge_base_id,
                environment_id, voice_id, skill_ids, welcome_message, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::TEXT[], $10, NOW(), NOW())
            RETURNING
                id, name, description, prompt, robot_id, knowledge_base_id,
                environment_id, voice_id, skill_ids, welcome_message, created_at, updated_at
            """,
            orchestration_id,
            name,
            description,
            prompt,
            robot_id,
            knowledge_base_id,
            environment_id,
            voice_id,
            skill_ids,
            welcome_message,
        )
        assert row is not None
        return _row_to_orchestration_record(row)

    async def get_orchestration(
        self, orchestration_id: str
    ) -> OrchestrationRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT
                id, name, description, prompt, robot_id, knowledge_base_id,
                environment_id, voice_id, skill_ids, welcome_message, created_at, updated_at
            FROM orchestration_tbl
            WHERE id = $1
            """,
            orchestration_id,
        )
        if row is None:
            return None
        return _row_to_orchestration_record(row)

    async def count_orchestrations(self) -> int:
        pool = self._require_pool()
        value = await pool.fetchval("SELECT COUNT(*) FROM orchestration_tbl")
        return int(value or 0)

    async def list_orchestrations(
        self, *, limit: int, offset: int
    ) -> list[OrchestrationRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                id, name, description, prompt, robot_id, knowledge_base_id,
                environment_id, voice_id, skill_ids, welcome_message, created_at, updated_at
            FROM orchestration_tbl
            ORDER BY updated_at DESC, created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        return [_row_to_orchestration_record(row) for row in rows]

    async def update_orchestration(
        self,
        *,
        orchestration_id: str,
        name: str,
        description: str,
        prompt: str,
        robot_id: str | None,
        knowledge_base_id: str | None,
        environment_id: str | None,
        voice_id: str | None,
        skill_ids: list[str],
        welcome_message: str,
    ) -> OrchestrationRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            UPDATE orchestration_tbl
            SET
                name = $2,
                description = $3,
                prompt = $4,
                robot_id = $5,
                knowledge_base_id = $6,
                environment_id = $7,
                voice_id = $8,
                skill_ids = $9::TEXT[],
                welcome_message = $10,
                updated_at = NOW()
            WHERE id = $1
            RETURNING
                id, name, description, prompt, robot_id, knowledge_base_id,
                environment_id, voice_id, skill_ids, welcome_message, created_at, updated_at
            """,
            orchestration_id,
            name,
            description,
            prompt,
            robot_id,
            knowledge_base_id,
            environment_id,
            voice_id,
            skill_ids,
            welcome_message,
        )
        if row is None:
            return None
        return _row_to_orchestration_record(row)

    async def delete_orchestration(self, orchestration_id: str) -> bool:
        pool = self._require_pool()
        result = await pool.execute(
            """
            DELETE FROM orchestration_tbl
            WHERE id = $1
            """,
            orchestration_id,
        )
        deleted_count = int(str(result).split()[-1])
        return deleted_count > 0

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS orchestration_tbl (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                robot_id TEXT NULL,
                knowledge_base_id TEXT NULL,
                environment_id TEXT NULL,
                voice_id TEXT NULL,
                skill_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                welcome_message TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_orchestration_tbl_updated_at
            ON orchestration_tbl (updated_at DESC)
            """,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("orchestration_tbl PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


def _row_to_orchestration_record(row: Any) -> OrchestrationRecord:
    return OrchestrationRecord(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        description=str(row["description"] or ""),
        prompt=str(row["prompt"] or ""),
        robot_id=str(row["robot_id"]) if row["robot_id"] is not None else None,
        knowledge_base_id=(
            str(row["knowledge_base_id"])
            if row["knowledge_base_id"] is not None
            else None
        ),
        environment_id=(
            str(row["environment_id"]) if row["environment_id"] is not None else None
        ),
        voice_id=str(row["voice_id"]) if row["voice_id"] is not None else None,
        skill_ids=tuple(str(item) for item in (row["skill_ids"] or [])),
        welcome_message=str(row["welcome_message"] or ""),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "OrchestrationRecord",
    "OrchestrationStore",
    "PostgresOrchestrationStore",
]
