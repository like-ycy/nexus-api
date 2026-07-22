"""VLA debug persistence store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from src.utils.logging import logger


@dataclass(frozen=True, slots=True)
class VLAPolicyServiceRecord:
    service_id: str
    robot_type: str
    name: str
    endpoint: str
    protocol: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class VLADebugStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def list_policy_services(
        self, robot_type: str
    ) -> list[VLAPolicyServiceRecord]: ...
    async def get_policy_service(
        self, service_id: str
    ) -> VLAPolicyServiceRecord | None: ...
    async def upsert_policy_service(
        self,
        *,
        service_id: str,
        robot_type: str,
        name: str,
        endpoint: str,
        protocol: str,
    ) -> VLAPolicyServiceRecord: ...
    async def mark_policy_service_used(self, service_id: str) -> None: ...
    async def list_instruction_history(
        self, robot_type: str
    ) -> list[dict[str, object]]: ...
    async def remember_instruction(
        self,
        *,
        robot_type: str,
        instruction: str,
        machine_id: str,
        policy_service_id: str,
    ) -> None: ...


class PostgresVLADebugStore:
    """PostgreSQL-backed VLA debug store."""

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
                "未安装 asyncpg，无法启用 VLA debug PostgreSQL 存储。请先执行 uv sync。"
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

    async def list_policy_services(
        self, robot_type: str
    ) -> list[VLAPolicyServiceRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                service_id, robot_type, name, endpoint, protocol,
                created_at, updated_at, last_used_at
            FROM vla_policy_service_tbl
            WHERE robot_type = $1
            ORDER BY updated_at DESC, created_at DESC
            """,
            robot_type,
        )
        return [_row_to_policy_service(row) for row in rows]

    async def get_policy_service(
        self, service_id: str
    ) -> VLAPolicyServiceRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT
                service_id, robot_type, name, endpoint, protocol,
                created_at, updated_at, last_used_at
            FROM vla_policy_service_tbl
            WHERE service_id = $1
            """,
            service_id,
        )
        if row is None:
            return None
        return _row_to_policy_service(row)

    async def upsert_policy_service(
        self,
        *,
        service_id: str,
        robot_type: str,
        name: str,
        endpoint: str,
        protocol: str,
    ) -> VLAPolicyServiceRecord:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO vla_policy_service_tbl (
                service_id, robot_type, name, endpoint, protocol, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            ON CONFLICT (service_id) DO UPDATE
            SET
                robot_type = EXCLUDED.robot_type,
                name = EXCLUDED.name,
                endpoint = EXCLUDED.endpoint,
                protocol = EXCLUDED.protocol,
                updated_at = NOW()
            RETURNING
                service_id, robot_type, name, endpoint, protocol,
                created_at, updated_at, last_used_at
            """,
            service_id,
            robot_type,
            name,
            endpoint,
            protocol,
        )
        assert row is not None
        return _row_to_policy_service(row)

    async def mark_policy_service_used(self, service_id: str) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            UPDATE vla_policy_service_tbl
            SET last_used_at = NOW(), updated_at = NOW()
            WHERE service_id = $1
            """,
            service_id,
        )

    async def list_instruction_history(
        self, robot_type: str
    ) -> list[dict[str, object]]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                robot_type, instruction_text, use_count, last_machine_id,
                last_policy_service_id, last_used_at
            FROM vla_instruction_history_tbl
            WHERE robot_type = $1
            ORDER BY last_used_at DESC, instruction_text ASC
            """,
            robot_type,
        )
        return [_row_to_instruction_history(row) for row in rows]

    async def remember_instruction(
        self,
        *,
        robot_type: str,
        instruction: str,
        machine_id: str,
        policy_service_id: str,
    ) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO vla_instruction_history_tbl (
                robot_type, instruction_text, use_count, last_machine_id,
                last_policy_service_id, created_at, last_used_at
            )
            VALUES ($1, $2, 1, $3, $4, NOW(), NOW())
            ON CONFLICT (robot_type, instruction_text) DO UPDATE
            SET
                use_count = vla_instruction_history_tbl.use_count + 1,
                last_machine_id = EXCLUDED.last_machine_id,
                last_policy_service_id = EXCLUDED.last_policy_service_id,
                last_used_at = NOW()
            """,
            robot_type,
            instruction,
            machine_id,
            policy_service_id,
        )

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS vla_policy_service_tbl (
                service_id TEXT PRIMARY KEY,
                robot_type TEXT NOT NULL,
                name TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                protocol TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at TIMESTAMPTZ NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vla_policy_service_tbl_robot_type_updated_at
            ON vla_policy_service_tbl (robot_type, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS vla_instruction_history_tbl (
                robot_type TEXT NOT NULL,
                instruction_text TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                last_machine_id TEXT NULL,
                last_policy_service_id TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (robot_type, instruction_text)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vla_instruction_history_tbl_robot_type_last_used
            ON vla_instruction_history_tbl (robot_type, last_used_at DESC)
            """,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("VLA debug PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


def _row_to_policy_service(row: Any) -> VLAPolicyServiceRecord:
    return VLAPolicyServiceRecord(
        service_id=str(row["service_id"]),
        robot_type=str(row["robot_type"]),
        name=str(row["name"]),
        endpoint=str(row["endpoint"]),
        protocol=str(row["protocol"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
    )


def _row_to_instruction_history(row: Any) -> dict[str, object]:
    return {
        "instruction_text": str(row["instruction_text"]),
        "robot_type": str(row["robot_type"]),
        "last_used_at": row["last_used_at"].isoformat(),
        "use_count": int(row["use_count"]),
        "last_machine_id": row["last_machine_id"],
        "last_policy_service_id": row["last_policy_service_id"],
    }


__all__ = [
    "PostgresVLADebugStore",
    "VLADebugStore",
    "VLAPolicyServiceRecord",
]
