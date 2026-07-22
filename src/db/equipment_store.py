"""equipments_tbl 存储抽象与 PostgreSQL 实现（embodied_cloud 库）。"""

from __future__ import annotations

from typing import Any, Protocol

from src.utils.logging import logger


class EquipmentRecord:
    """equipments_tbl 中的单台设备。"""

    __slots__ = (
        "uid",
        "name",
        "desc",
        "address",
        "status",
        "last_online",
    )

    def __init__(
        self,
        *,
        uid: str,
        name: str,
        desc: str,
        address: str,
        status: bool,
        last_online: int,
    ) -> None:
        self.uid = uid
        self.name = name
        self.desc = desc
        self.address = address
        self.status = status
        self.last_online = last_online


class EquipmentStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def list_equipment(self) -> list[EquipmentRecord]: ...
    async def get_equipment(self, uid: str) -> EquipmentRecord | None: ...


class PostgresEquipmentStore:
    """基于 PostgreSQL 的 equipments_tbl 存储（embodied_cloud 库）。"""

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
                "未安装 asyncpg，无法启用 equipment PostgreSQL 存储。请先执行 uv sync。"
            ) from exc
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            command_timeout=self._command_timeout_sec,
        )
        logger.info("equipments_tbl PostgreSQL 存储已就绪（embodied_cloud）")

    async def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def list_equipment(self) -> list[EquipmentRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT uid, name, "desc", address, status, last_online
            FROM equipments_tbl
            WHERE is_deleted = FALSE
            ORDER BY last_online DESC
            """,
        )
        return [_row_to_equipment_record(row) for row in rows]

    async def get_equipment(self, uid: str) -> EquipmentRecord | None:
        pool = self._require_pool()
        row = await pool.fetchrow(
            """
            SELECT uid, name, "desc", address, status, last_online
            FROM equipments_tbl
            WHERE uid = $1 AND is_deleted = FALSE
            """,
            uid,
        )
        if row is None:
            return None
        return _row_to_equipment_record(row)

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化（embodied_cloud）")
        return self._pool


def _row_to_equipment_record(row: Any) -> EquipmentRecord:
    return EquipmentRecord(
        uid=str(row["uid"]),
        name=str(row["name"] or ""),
        desc=str(row["desc"] or ""),
        address=str(row["address"] or ""),
        status=bool(row["status"]),
        last_online=int(row["last_online"] or 0),
    )


__all__ = ["EquipmentRecord", "EquipmentStore", "PostgresEquipmentStore"]
