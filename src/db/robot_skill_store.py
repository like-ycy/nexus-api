"""robot_skill_tbl 存储抽象与 PostgreSQL 实现。"""

from __future__ import annotations

import json

from typing import Any, Protocol

from src.domain import RobotSkillRecord
from src.utils.logging import logger


class RobotSkillStore(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def replace_robot_skills(
        self,
        *,
        robot_id: str,
        skills: list[RobotSkillRecord],
    ) -> None: ...
    async def list_robot_skills(self, robot_id: str) -> list[RobotSkillRecord]: ...


class PostgresRobotSkillStore:
    """基于 PostgreSQL 的 robot_skill_tbl 存储。"""

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
                "未安装 asyncpg，无法启用 robot_skill PostgreSQL 存储。请先执行 uv sync。"
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

    async def replace_robot_skills(
        self,
        *,
        robot_id: str,
        skills: list[RobotSkillRecord],
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM robot_skill_tbl
                    WHERE robot_id = $1
                    """,
                    robot_id,
                )
                if not skills:
                    return
                await conn.executemany(
                    """
                    INSERT INTO robot_skill_tbl (
                        robot_id,
                        skill_id,
                        skill_name,
                        tool_name,
                        description,
                        input_schema_json,
                        output_schema_json,
                        supports_cancel,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6::JSONB, $7::JSONB, $8,
                        NOW(), NOW()
                    )
                    """,
                    [
                        (
                            robot_id,
                            skill.skill_id,
                            skill.skill_name,
                            skill.tool_name,
                            skill.description,
                            json.dumps(skill.input_schema, ensure_ascii=False),
                            (
                                None
                                if skill.output_schema is None
                                else json.dumps(skill.output_schema, ensure_ascii=False)
                            ),
                            skill.supports_cancel,
                        )
                        for skill in skills
                    ],
                )

    async def list_robot_skills(self, robot_id: str) -> list[RobotSkillRecord]:
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT
                robot_id,
                skill_id,
                skill_name,
                tool_name,
                description,
                input_schema_json,
                output_schema_json,
                supports_cancel
            FROM robot_skill_tbl
            WHERE robot_id = $1
            ORDER BY skill_id ASC
            """,
            robot_id,
        )
        return [_row_to_robot_skill_record(row) for row in rows]

    async def _initialize_schema(self) -> None:
        pool = self._require_pool()
        statements = (
            """
            CREATE TABLE IF NOT EXISTS robot_skill_tbl (
                robot_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                input_schema_json JSONB NOT NULL DEFAULT '{}'::JSONB,
                output_schema_json JSONB NULL,
                supports_cancel BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (robot_id, skill_id),
                UNIQUE (robot_id, tool_name)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_robot_skill_tbl_robot_id
            ON robot_skill_tbl (robot_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_robot_skill_tbl_updated_at
            ON robot_skill_tbl (updated_at DESC)
            """,
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in statements:
                    await conn.execute(statement)
        logger.info("robot_skill_tbl PostgreSQL 存储已就绪")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool


def _row_to_robot_skill_record(row: Any) -> RobotSkillRecord:
    input_schema = row["input_schema_json"]
    output_schema = row["output_schema_json"]
    return RobotSkillRecord(
        robot_id=str(row["robot_id"]),
        skill_id=str(row["skill_id"]),
        skill_name=str(row["skill_name"]),
        tool_name=str(row["tool_name"]),
        description=str(row["description"] or ""),
        input_schema=input_schema if isinstance(input_schema, dict) else {},
        output_schema=output_schema if isinstance(output_schema, dict) else None,
        supports_cancel=bool(row["supports_cancel"]),
    )


__all__ = ["PostgresRobotSkillStore", "RobotSkillStore"]
