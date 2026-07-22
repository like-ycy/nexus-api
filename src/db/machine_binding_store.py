"""machine_binding_tbl 存储抽象与 PostgreSQL 实现。"""

from __future__ import annotations

from typing import Any, Protocol

from src.domain import MachineBindingRecord
from src.utils.logging import logger


class MachineBindingStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def get_binding(self, machine_id: str) -> MachineBindingRecord | None: ...
    async def list_bindings(self) -> list[MachineBindingRecord]: ...
    async def upsert_binding(
        self,
        *,
        machine_id: str,
        orchestration_id: str | None,
    ) -> MachineBindingRecord: ...


class PostgresMachineBindingStore:
    """基于 PostgreSQL 的 machine_binding_tbl 存储。"""

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
                "未安装 asyncpg，无法启用 machine_binding PostgreSQL 存储。请先执行 uv sync。"
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

    async def get_binding(self, machine_id: str) -> MachineBindingRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT
                machine_id,
                orchestration_id,
                binding_version,
                created_at,
                updated_at
            FROM machine_binding_tbl
            WHERE machine_id = $1
            """,
            machine_id,
        )
        if row is None:
            return None
        return _row_to_machine_binding_record(row)

    async def list_bindings(self) -> list[MachineBindingRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                machine_id,
                orchestration_id,
                binding_version,
                created_at,
                updated_at
            FROM machine_binding_tbl
            ORDER BY updated_at DESC, created_at DESC
            """
        )
        return [_row_to_machine_binding_record(row) for row in rows]

    async def upsert_binding(
        self,
        *,
        machine_id: str,
        orchestration_id: str | None,
    ) -> MachineBindingRecord:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            INSERT INTO machine_binding_tbl (
                machine_id,
                orchestration_id,
                binding_version,
                created_at,
                updated_at
            )
            VALUES ($1, $2, 1, NOW(), NOW())
            ON CONFLICT (machine_id) DO UPDATE
            SET
                orchestration_id = EXCLUDED.orchestration_id,
                binding_version = machine_binding_tbl.binding_version + 1,
                updated_at = NOW()
            RETURNING
                machine_id,
                orchestration_id,
                binding_version,
                created_at,
                updated_at
            """,
            machine_id,
            orchestration_id,
        )
        assert row is not None
        return _row_to_machine_binding_record(row)

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS machine_binding_tbl (
                machine_id TEXT PRIMARY KEY,
                orchestration_id TEXT NULL,
                binding_version INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_machine_binding_tbl_updated_at
            ON machine_binding_tbl (updated_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_machine_binding_tbl_orchestration_id
            ON machine_binding_tbl (orchestration_id)
            """,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("machine_binding_tbl PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


def _row_to_machine_binding_record(row: Any) -> MachineBindingRecord:
    return MachineBindingRecord(
        machine_id=str(row["machine_id"]),
        orchestration_id=(
            str(row["orchestration_id"]).strip()
            if row["orchestration_id"] is not None
            else None
        ),
        binding_version=int(row["binding_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = ["MachineBindingStore", "PostgresMachineBindingStore"]
