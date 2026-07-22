"""在线设备连接注册表。"""

from __future__ import annotations

import asyncio

from typing import Protocol


class BindingConnection(Protocol):
    async def send_binding_message(self, payload: dict[str, object]) -> None: ...
    async def send_control_message(self, payload: dict[str, object]) -> None: ...
    async def invoke_edge_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object] | None,
        session_id: str | None,
        timeout_ms: int,
    ) -> dict[str, object]: ...
    async def start_edge_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object] | None,
        session_id: str | None,
        timeout_ms: int,
    ) -> str: ...
    async def cancel_edge_tool(
        self,
        *,
        invocation_id: str,
        tool_name: str,
    ) -> None: ...
    async def send_vla_debug_camera_subscription(
        self,
        *,
        subscription_id: str,
        fps: float,
        subscribe: bool,
    ) -> None: ...


class ConnectionRegistry:
    """维护 machine_id 到在线连接的映射。"""

    def __init__(self) -> None:
        self._connections: dict[str, BindingConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, machine_id: str, connection: BindingConnection) -> None:
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return
        async with self._lock:
            self._connections[normalized_machine_id] = connection

    async def unregister(
        self,
        machine_id: str,
        connection: BindingConnection | None = None,
    ) -> None:
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return
        async with self._lock:
            current = self._connections.get(normalized_machine_id)
            if current is None:
                return
            if connection is not None and current is not connection:
                return
            self._connections.pop(normalized_machine_id, None)

    async def get(self, machine_id: str) -> BindingConnection | None:
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return None
        async with self._lock:
            return self._connections.get(normalized_machine_id)

    async def is_online(self, machine_id: str) -> bool:
        return await self.get(machine_id) is not None

    async def list_machine_ids(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(self._connections.keys())

    async def push(self, machine_id: str, payload: dict[str, object]) -> bool:
        connection = await self.get(machine_id)
        if connection is None:
            return False
        await connection.send_binding_message(payload)
        return True

    async def push_control(self, machine_id: str, payload: dict[str, object]) -> None:
        connection = await self.get(machine_id)
        if connection is None:
            raise RuntimeError("machine is offline")
        await connection.send_control_message(payload)

    async def invoke_edge_tool(
        self,
        machine_id: str,
        *,
        tool_name: str,
        arguments: dict[str, object] | None,
        session_id: str | None = None,
        timeout_ms: int = 60_000,
    ) -> dict[str, object]:
        connection = await self.get(machine_id)
        if connection is None:
            raise RuntimeError("machine is offline")
        return await connection.invoke_edge_tool(
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            timeout_ms=timeout_ms,
        )

    async def start_edge_tool(
        self,
        machine_id: str,
        *,
        tool_name: str,
        arguments: dict[str, object] | None,
        session_id: str | None = None,
        timeout_ms: int = 0,
    ) -> str:
        connection = await self.get(machine_id)
        if connection is None:
            raise RuntimeError("machine is offline")
        return await connection.start_edge_tool(
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
            timeout_ms=timeout_ms,
        )

    async def cancel_edge_tool(
        self,
        machine_id: str,
        *,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        connection = await self.get(machine_id)
        if connection is None:
            raise RuntimeError("machine is offline")
        await connection.cancel_edge_tool(
            invocation_id=invocation_id,
            tool_name=tool_name,
        )

    async def send_vla_debug_camera_subscription(
        self,
        machine_id: str,
        *,
        subscription_id: str,
        fps: float,
        subscribe: bool,
    ) -> None:
        connection = await self.get(machine_id)
        if connection is None:
            raise RuntimeError("machine is offline")
        await connection.send_vla_debug_camera_subscription(
            subscription_id=subscription_id,
            fps=fps,
            subscribe=subscribe,
        )


__all__ = ["BindingConnection", "ConnectionRegistry"]
