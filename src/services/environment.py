"""环境管理服务。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from src.db.environment_store import EnvironmentStore, PostgresEnvironmentStore
from src.domain import EnvironmentPointRecord, EnvironmentRecord


class EnvironmentNotFoundError(KeyError):
    """环境不存在。"""


class EnvironmentPointNotFoundError(KeyError):
    """环境点位不存在。"""


class EnvironmentService:
    """环境与点位管理。"""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        store: EnvironmentStore | None = None,
    ) -> None:
        self._store = store if store is not None else _build_environment_store(config)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._store.open()
        self._started = True

    async def close(self) -> None:
        started = self._started
        self._started = False
        if started:
            await self._store.close()

    async def list_environments(self) -> list[dict[str, Any]]:
        await self.start()
        records = await self._store.list_environments()
        return [_serialize_environment_summary(item) for item in records]

    async def create_environment(
        self,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        await self.start()
        record = await self._store.create_environment(
            environment_id=uuid4().hex,
            name=name.strip(),
            description=description.strip(),
            extra={},
        )
        return _serialize_environment_detail(record)

    async def get_environment(self, environment_id: str) -> dict[str, Any]:
        await self.start()
        record = await self._store.get_environment(environment_id)
        if record is None:
            raise EnvironmentNotFoundError(environment_id)
        return _serialize_environment_detail(record)

    async def update_environment(
        self,
        environment_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        await self.start()
        record = await self._store.update_environment(
            environment_id=environment_id,
            name=name.strip(),
            description=description.strip(),
            extra={},
        )
        if record is None:
            raise EnvironmentNotFoundError(environment_id)
        detail = await self._store.get_environment(environment_id)
        if detail is None:
            raise EnvironmentNotFoundError(environment_id)
        return _serialize_environment_detail(detail)

    async def delete_environment(self, environment_id: str) -> dict[str, Any]:
        await self.start()
        deleted = await self._store.delete_environment(environment_id)
        if not deleted:
            raise EnvironmentNotFoundError(environment_id)
        return {"id": environment_id, "deleted": True}

    async def create_environment_point(
        self,
        environment_id: str,
        *,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> dict[str, Any]:
        await self.start()
        environment = await self._store.get_environment(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(environment_id)
        point = await self._store.create_environment_point(
            point_id=uuid4().hex,
            environment_id=environment_id,
            name=name.strip(),
            tag=tag.strip(),
            description=description.strip(),
            sort_order=sort_order,
        )
        assert point is not None
        return _serialize_environment_point(point)

    async def update_environment_point(
        self,
        environment_id: str,
        point_id: str,
        *,
        name: str,
        tag: str,
        description: str,
        sort_order: int,
    ) -> dict[str, Any]:
        await self.start()
        point = await self._store.update_environment_point(
            point_id=point_id,
            environment_id=environment_id,
            name=name.strip(),
            tag=tag.strip(),
            description=description.strip(),
            sort_order=sort_order,
        )
        if point is None:
            environment = await self._store.get_environment(environment_id)
            if environment is None:
                raise EnvironmentNotFoundError(environment_id)
            raise EnvironmentPointNotFoundError(point_id)
        return _serialize_environment_point(point)

    async def delete_environment_point(
        self, environment_id: str, point_id: str
    ) -> dict[str, Any]:
        await self.start()
        deleted = await self._store.delete_environment_point(
            point_id=point_id,
            environment_id=environment_id,
        )
        if deleted:
            return {"id": point_id, "environment_id": environment_id, "deleted": True}
        environment = await self._store.get_environment(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(environment_id)
        raise EnvironmentPointNotFoundError(point_id)


def _build_environment_store(config: dict[str, Any]) -> EnvironmentStore:
    database_config = _require_section(config, "database")
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        raise ValueError("environment service requires database.dsn")
    return PostgresEnvironmentStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


def _serialize_environment_summary(record: EnvironmentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "created_at": int(record.created_at.timestamp()),
        "updated_at": int(record.updated_at.timestamp()),
    }


def _serialize_environment_detail(record: EnvironmentRecord) -> dict[str, Any]:
    payload = _serialize_environment_summary(record)
    payload["points"] = [_serialize_environment_point(item) for item in record.points]
    return payload


def _serialize_environment_point(record: EnvironmentPointRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "environment_id": record.environment_id,
        "name": record.name,
        "tag": record.tag,
        "description": record.description,
        "sort_order": record.sort_order,
        "created_at": int(record.created_at.timestamp()),
        "updated_at": int(record.updated_at.timestamp()),
    }


__all__ = [
    "EnvironmentNotFoundError",
    "EnvironmentPointNotFoundError",
    "EnvironmentService",
]
