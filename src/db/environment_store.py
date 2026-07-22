"""environment_tbl / environment_point_tbl 存储抽象与 PostgreSQL 实现。"""

from __future__ import annotations

import json

from typing import Any, Protocol

from src.domain import EnvironmentPointRecord, EnvironmentRecord
from src.utils.logging import logger


class EnvironmentStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def list_environments(self) -> list[EnvironmentRecord]: ...
    async def create_environment(
        self,
        *,
        environment_id: str,
        name: str,
        description: str,
        extra: dict[str, Any],
    ) -> EnvironmentRecord: ...
    async def get_environment(
        self, environment_id: str
    ) -> EnvironmentRecord | None: ...
    async def update_environment(
        self,
        *,
        environment_id: str,
        name: str,
        description: str,
        extra: dict[str, Any],
    ) -> EnvironmentRecord | None: ...
    async def delete_environment(self, environment_id: str) -> bool: ...
    async def create_environment_point(
        self,
        *,
        point_id: str,
        environment_id: str,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> EnvironmentPointRecord | None: ...
    async def update_environment_point(
        self,
        *,
        point_id: str,
        environment_id: str,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> EnvironmentPointRecord | None: ...
    async def delete_environment_point(
        self, *, point_id: str, environment_id: str
    ) -> bool: ...


class PostgresEnvironmentStore:
    """基于 PostgreSQL 的环境存储。"""

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
                "未安装 asyncpg，无法启用 environment PostgreSQL 存储。请先执行 uv sync。"
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

    async def list_environments(self) -> list[EnvironmentRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                id, name, description, extra, created_at, updated_at
            FROM environment_tbl
            ORDER BY updated_at DESC, created_at DESC
            """
        )
        return [_row_to_environment_record(row) for row in rows]

    async def create_environment(
        self,
        *,
        environment_id: str,
        name: str,
        description: str,
        extra: dict[str, Any],
    ) -> EnvironmentRecord:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO environment_tbl (
                id, name, description, extra, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4::JSONB, NOW(), NOW())
            RETURNING
                id, name, description, extra, created_at, updated_at
            """,
            environment_id,
            name,
            description,
            json.dumps(extra, ensure_ascii=False),
        )
        assert row is not None
        return _row_to_environment_record(row)

    async def get_environment(self, environment_id: str) -> EnvironmentRecord | None:
        pool = self._require_pool()
        env_row = await pool.fetchrow(
            """
            SELECT
                id, name, description, extra, created_at, updated_at
            FROM environment_tbl
            WHERE id = $1
            """,
            environment_id,
        )
        if env_row is None:
            return None
        point_rows = await pool.fetch(
            """
            SELECT
                id, environment_id, name, tag, description, sort_order, created_at, updated_at
            FROM environment_point_tbl
            WHERE environment_id = $1
            ORDER BY sort_order ASC, created_at ASC
            """,
            environment_id,
        )
        return _row_to_environment_record(
            env_row,
            points=[_row_to_environment_point_record(row) for row in point_rows],
        )

    async def update_environment(
        self,
        *,
        environment_id: str,
        name: str,
        description: str,
        extra: dict[str, Any],
    ) -> EnvironmentRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            UPDATE environment_tbl
            SET
                name = $2,
                description = $3,
                extra = $4::JSONB,
                updated_at = NOW()
            WHERE id = $1
            RETURNING
                id, name, description, extra, created_at, updated_at
            """,
            environment_id,
            name,
            description,
            json.dumps(extra, ensure_ascii=False),
        )
        if row is None:
            return None
        return _row_to_environment_record(row)

    async def delete_environment(self, environment_id: str) -> bool:
        pool = self._require_pool()
        result = await pool.execute(
            "DELETE FROM environment_tbl WHERE id = $1",
            environment_id,
        )
        return result.endswith("1")

    async def create_environment_point(
        self,
        *,
        point_id: str,
        environment_id: str,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> EnvironmentPointRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO environment_point_tbl (
                id, environment_id, name, tag, description, sort_order, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
            RETURNING
                id, environment_id, name, tag, description, sort_order, created_at, updated_at
            """,
            point_id,
            environment_id,
            name,
            tag,
            description,
            sort_order,
        )
        if row is None:
            return None
        return _row_to_environment_point_record(row)

    async def update_environment_point(
        self,
        *,
        point_id: str,
        environment_id: str,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> EnvironmentPointRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            UPDATE environment_point_tbl
            SET
                name = $3,
                tag = $4,
                description = $5,
                sort_order = $6,
                updated_at = NOW()
            WHERE id = $1 AND environment_id = $2
            RETURNING
                id, environment_id, name, tag, description, sort_order, created_at, updated_at
            """,
            point_id,
            environment_id,
            name,
            tag,
            description,
            sort_order,
        )
        if row is None:
            return None
        return _row_to_environment_point_record(row)

    async def delete_environment_point(
        self, *, point_id: str, environment_id: str
    ) -> bool:
        pool = self._require_pool()
        result = await pool.execute(
            "DELETE FROM environment_point_tbl WHERE id = $1 AND environment_id = $2",
            point_id,
            environment_id,
        )
        return result.endswith("1")

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS environment_tbl (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                extra JSONB NOT NULL DEFAULT '{}'::JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS environment_point_tbl (
                id TEXT PRIMARY KEY,
                environment_id TEXT NOT NULL REFERENCES environment_tbl(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                tag TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_environment_tbl_updated_at
            ON environment_tbl (updated_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_environment_point_tbl_environment_id_sort
            ON environment_point_tbl (environment_id, sort_order ASC, created_at ASC)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uk_environment_point_tbl_env_name
            ON environment_point_tbl (environment_id, name)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uk_environment_point_tbl_env_tag
            ON environment_point_tbl (environment_id, tag)
            """,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("environment PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


def _row_to_environment_record(
    row: Any,
    *,
    points: list[EnvironmentPointRecord] | None = None,
) -> EnvironmentRecord:
    extra = row["extra"]
    if not isinstance(extra, dict):
        extra = {}
    return EnvironmentRecord(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        description=str(row["description"] or ""),
        extra=extra,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        points=tuple(points or ()),
    )


def _row_to_environment_point_record(row: Any) -> EnvironmentPointRecord:
    return EnvironmentPointRecord(
        id=str(row["id"]),
        environment_id=str(row["environment_id"]),
        name=str(row["name"] or ""),
        tag=str(row["tag"] or ""),
        description=str(row["description"] or ""),
        sort_order=int(row["sort_order"] or 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "EnvironmentStore",
    "PostgresEnvironmentStore",
]
