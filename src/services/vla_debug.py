"""VLA model debug service."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from urllib.parse import quote
from typing import Any

from src.core.connection_registry import ConnectionRegistry
from src.db.vla_debug_store import VLADebugStore, VLAPolicyServiceRecord
from src.services.orchestration import OrchestrationService
from src.utils.logging import logger

EDGE_TOOL_VLA_CONTROL = "vla.control"
DEFAULT_POLICY_PROTOCOL = "ainno_grpc_v1"
EDGE_EXECUTION_MODE_BY_API_MODE = {
    "sync": "sync_run_to_completion",
    "async": "async_replace_pending",
}
SUPPORTED_EXECUTION_SPACES = {"joint", "eef"}
RESET_TOOL_TIMEOUT_MS = 120_000
STOP_TOOL_TIMEOUT_MS = 120_000
DEFAULT_CAMERA_FPS = 2.0
MAX_CAMERA_FPS = 15.0
CAMERA_QUEUE_SIZE = 4
LIVE_CAMERA_STATE_PUBLISHING = "publishing"
LIVE_CAMERA_STATE_STOPPED = "stopped"
LIVE_CAMERA_STATES = {LIVE_CAMERA_STATE_PUBLISHING, LIVE_CAMERA_STATE_STOPPED}
DEFAULT_SRS_WEBRTC_BASE_URL = "http://192.168.21.138:1985"
DEFAULT_SRS_HTTP_BASE_URL = "http://192.168.21.138:18080"
DEFAULT_SRS_APP = "live"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VLADebugError(RuntimeError):
    """Raised when a VLA debug operation cannot be completed."""

    def __init__(self, message: str, *, code: str = "vla_debug_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ActiveVLATask:
    task_id: str
    machine_id: str
    robot_type: str
    policy_service_id: str
    instruction: str
    execution_space: str
    execution_mode: str
    invocation_id: str | None
    status: str
    started_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LiveCameraConfig:
    srs_webrtc_base_url: str = DEFAULT_SRS_WEBRTC_BASE_URL
    srs_http_base_url: str = DEFAULT_SRS_HTTP_BASE_URL
    srs_app: str = DEFAULT_SRS_APP


class VLADebugService:
    """Coordinates VLA model debugging through online Nexus Edge connections."""

    def __init__(
        self,
        *,
        orchestration_service: OrchestrationService,
        connection_registry: ConnectionRegistry,
        store: VLADebugStore | None = None,
        live_camera_config: dict[str, Any] | None = None,
    ) -> None:
        self._orchestration_service = orchestration_service
        self._connection_registry = connection_registry
        self._store = store
        self._live_camera_config = _build_live_camera_config(live_camera_config)
        self._policy_services: dict[str, VLAPolicyServiceRecord] = {}
        self._instruction_history: dict[str, dict[str, Any]] = {}
        self._active_tasks_by_machine: dict[str, ActiveVLATask] = {}
        self._latest_camera_frames: dict[str, dict[str, object]] = {}
        self._camera_subscribers: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}
        self._live_camera_status_by_machine: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._store is None:
            return
        async with self._start_lock:
            if self._started:
                return
            await self._store.open()
            self._started = True

    async def close(self) -> None:
        if self._store is None:
            return
        async with self._start_lock:
            started = self._started
            self._started = False
        if started:
            await self._store.close()

    async def list_robot_types(self) -> list[dict[str, object]]:
        machines = await self._list_machines()
        by_type: dict[str, dict[str, int]] = {}
        for machine in machines:
            robot_type = _normalize_robot_type(machine.get("type_name"))
            if robot_type is None:
                continue
            stats = by_type.setdefault(robot_type, {"machine_count": 0, "online_count": 0})
            stats["machine_count"] += 1
            if machine.get("is_online") is True:
                stats["online_count"] += 1
        return [
            {
                "robot_type": robot_type,
                "machine_count": stats["machine_count"],
                "online_count": stats["online_count"],
            }
            for robot_type, stats in sorted(by_type.items())
        ]

    async def list_all_machines(self) -> list[dict[str, object]]:
        machines = []
        for machine in await self._list_machines():
            robot_type = _normalize_robot_type(machine.get("type_name"))
            if robot_type is None:
                continue
            item = dict(machine)
            item["robot_type"] = robot_type
            item["can_start_vla"] = True
            machines.append(item)
        return sorted(
            machines,
            key=lambda item: (
                str(item.get("robot_type") or ""),
                str(item.get("name") or item.get("machine_id") or ""),
            ),
        )

    async def list_machines(self, robot_type: str) -> list[dict[str, object]]:
        normalized_robot_type = _require_robot_type(robot_type)
        machines = []
        for machine in await self._list_machines():
            if _normalize_robot_type(machine.get("type_name")) != normalized_robot_type:
                continue
            item = dict(machine)
            item["robot_type"] = normalized_robot_type
            item["can_start_vla"] = True
            machines.append(item)
        return machines

    async def list_policy_services(self, robot_type: str) -> list[dict[str, object]]:
        normalized_robot_type = _require_robot_type(robot_type)
        if self._store is not None:
            await self.start()
            return [
                _serialize_policy_service(record)
                for record in await self._store.list_policy_services(
                    normalized_robot_type
                )
            ]
        return [
            _serialize_policy_service(record)
            for record in sorted(
                self._policy_services.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
            if record.robot_type == normalized_robot_type
        ]

    async def save_policy_service(
        self,
        *,
        robot_type: str,
        name: str,
        endpoint: str,
        service_id: str | None = None,
    ) -> dict[str, object]:
        normalized_robot_type = _require_robot_type(robot_type)
        normalized_name = _require_text(name, "name")
        normalized_endpoint = _require_text(endpoint, "endpoint")
        now = _now()
        normalized_service_id = service_id.strip() if service_id else uuid.uuid4().hex
        if self._store is not None:
            await self.start()
            current = await self._store.get_policy_service(normalized_service_id)
            record = await self._store.upsert_policy_service(
                service_id=normalized_service_id,
                robot_type=normalized_robot_type,
                name=normalized_name,
                endpoint=normalized_endpoint,
                protocol=current.protocol if current else DEFAULT_POLICY_PROTOCOL,
            )
            return _serialize_policy_service(record)
        current = self._policy_services.get(normalized_service_id)
        record = VLAPolicyServiceRecord(
            service_id=normalized_service_id,
            robot_type=normalized_robot_type,
            name=normalized_name,
            endpoint=normalized_endpoint,
            protocol=current.protocol if current else DEFAULT_POLICY_PROTOCOL,
            created_at=current.created_at if current else now,
            updated_at=now,
            last_used_at=current.last_used_at if current else None,
        )
        self._policy_services[record.service_id] = record
        return _serialize_policy_service(record)

    async def list_instruction_history(self, robot_type: str) -> list[dict[str, object]]:
        normalized_robot_type = _require_robot_type(robot_type)
        if self._store is not None:
            await self.start()
            return await self._store.list_instruction_history(normalized_robot_type)
        items = [
            item
            for item in self._instruction_history.values()
            if item["robot_type"] == normalized_robot_type
        ]
        return sorted(items, key=lambda item: item["last_used_at"], reverse=True)

    async def start_task(
        self,
        *,
        robot_type: str,
        machine_id: str,
        policy_service_id: str,
        instruction: str,
        execution_space: str,
        execution_mode: str = "sync",
    ) -> dict[str, object]:
        normalized_robot_type = _require_robot_type(robot_type)
        normalized_machine_id = _require_text(machine_id, "machine_id")
        normalized_instruction = _require_text(instruction, "instruction")
        normalized_execution_space = _normalize_execution_space(execution_space)
        normalized_execution_mode = _normalize_execution_mode(execution_mode)
        edge_execution_mode = EDGE_EXECUTION_MODE_BY_API_MODE[normalized_execution_mode]
        record = await self._get_policy_service(
            _require_text(policy_service_id, "policy_service_id")
        )
        if record is None:
            raise VLADebugError("VLA policy service not found", code="policy_service_not_found")
        if record.robot_type != normalized_robot_type:
            raise VLADebugError("policy service robot_type mismatch", code="robot_type_mismatch")
        await self._validate_machine(normalized_machine_id, normalized_robot_type)
        async with self._lock:
            if normalized_machine_id in self._active_tasks_by_machine:
                raise VLADebugError("machine already has active VLA task", code="active_task_exists")
            task = ActiveVLATask(
                task_id=uuid.uuid4().hex,
                machine_id=normalized_machine_id,
                robot_type=normalized_robot_type,
                policy_service_id=record.service_id,
                instruction=normalized_instruction,
                execution_space=normalized_execution_space,
                execution_mode=normalized_execution_mode,
                invocation_id=None,
                status="starting",
                started_at=_now(),
                updated_at=_now(),
            )
            self._active_tasks_by_machine[normalized_machine_id] = task
        try:
            invocation_id = await self._connection_registry.start_edge_tool(
                normalized_machine_id,
                tool_name=EDGE_TOOL_VLA_CONTROL,
                session_id=None,
                timeout_ms=0,
                arguments={
                    "command": "run",
                    "instruction": normalized_instruction,
                    "execution_space": normalized_execution_space,
                    "execution_mode": edge_execution_mode,
                    "policy_endpoint": record.endpoint,
                    "policy_protocol": record.protocol,
                    "policy_robot_type": normalized_robot_type,
                },
            )
        except Exception:
            async with self._lock:
                self._active_tasks_by_machine.pop(normalized_machine_id, None)
            raise
        if self._store is not None:
            await self.start()
            await self._store.mark_policy_service_used(record.service_id)
        else:
            self._mark_memory_policy_service_used(record)
        task.status = "running"
        task.updated_at = _now()
        task.invocation_id = invocation_id
        await self._remember_instruction(
            robot_type=normalized_robot_type,
            instruction=normalized_instruction,
            machine_id=normalized_machine_id,
            policy_service_id=record.service_id,
        )
        return _serialize_task(task)

    async def stop_task(self, task_id: str) -> dict[str, object]:
        task = self._find_task(_require_text(task_id, "task_id"))
        await self._stop_active_task(task)
        return _serialize_task(task)

    async def reset_machine(self, machine_id: str) -> dict[str, object]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        task = self._active_tasks_by_machine.get(normalized_machine_id)
        if task is not None:
            await self._stop_active_task(task)
        response = await self._connection_registry.invoke_edge_tool(
            normalized_machine_id,
            tool_name=EDGE_TOOL_VLA_CONTROL,
            session_id=None,
            timeout_ms=RESET_TOOL_TIMEOUT_MS,
            arguments={"command": "reset", "reason": "vla_debug_reset"},
        )
        return {"machine_id": normalized_machine_id, "runtime_response": response}

    async def get_machine_state(self, machine_id: str) -> dict[str, object]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        machine = await self._find_machine(normalized_machine_id)
        return {
            "machine": machine,
            "active_task": _serialize_task(self._active_tasks_by_machine[normalized_machine_id])
            if normalized_machine_id in self._active_tasks_by_machine
            else None,
        }

    async def get_live_camera(self, machine_id: str) -> dict[str, object]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        machine = await self._find_machine_or_raise(normalized_machine_id)
        return self._build_live_camera_response(normalized_machine_id, machine=machine)

    async def start_live_camera(self, machine_id: str) -> dict[str, object]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        await self._find_machine_or_raise(normalized_machine_id)
        await self._connection_registry.push_control(
            normalized_machine_id,
            {
                "type": "live_camera.start",
                "machine_id": normalized_machine_id,
            },
        )
        return {
            "machine_id": normalized_machine_id,
            "state": self._current_live_camera_state(normalized_machine_id),
            "command": "start",
        }

    async def stop_live_camera(self, machine_id: str) -> dict[str, object]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        await self._find_machine_or_raise(normalized_machine_id)
        await self._connection_registry.push_control(
            normalized_machine_id,
            {
                "type": "live_camera.stop",
                "machine_id": normalized_machine_id,
            },
        )
        return {
            "machine_id": normalized_machine_id,
            "state": self._current_live_camera_state(normalized_machine_id),
            "command": "stop",
        }

    async def handle_live_camera_status(
        self,
        *,
        machine_id: str,
        status: dict[str, object],
    ) -> None:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        normalized_status = dict(status)
        normalized_status["state"] = _normalize_live_camera_state(
            normalized_status.get("state")
        )
        normalized_status["machine_id"] = normalized_machine_id
        normalized_status["updated_at"] = _now().isoformat()
        self._live_camera_status_by_machine[normalized_machine_id] = normalized_status

    async def handle_debug_camera_frame(
        self,
        *,
        machine_id: str,
        frame: dict[str, object],
    ) -> None:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        payload = dict(frame)
        self._latest_camera_frames[normalized_machine_id] = payload
        views = payload.get("views")
        view_count = len(views) if isinstance(views, list) else 0
        total_payload_chars = 0
        first_view_names: list[str] = []
        if isinstance(views, list):
            for view in views:
                if not isinstance(view, dict):
                    continue
                if len(first_view_names) < 3:
                    first_view_names.append(str(view.get("name") or ""))
                data = view.get("data")
                if isinstance(data, str):
                    total_payload_chars += len(data)
        logger.info(
            (
                "VLA 调试相机帧已接收 | machine_id={} subscription_id={} "
                "views={} payload_chars={} first_views={} subscribers={}"
            ),
            normalized_machine_id,
            payload.get("subscription_id"),
            view_count,
            total_payload_chars,
            first_view_names,
            len(self._camera_subscribers.get(normalized_machine_id, ())),
        )
        for queue in list(self._camera_subscribers.get(normalized_machine_id, ())):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def stream_camera_frames(
        self,
        machine_id: str,
        *,
        fps: float = DEFAULT_CAMERA_FPS,
    ) -> AsyncIterator[dict[str, object]]:
        normalized_machine_id = _require_text(machine_id, "machine_id")
        await self._find_machine_or_raise(normalized_machine_id)
        subscription_id = f"vla-debug-camera:{normalized_machine_id}"
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=CAMERA_QUEUE_SIZE
        )
        async with self._lock:
            subscribers = self._camera_subscribers.setdefault(
                normalized_machine_id, set()
            )
            subscribers.add(queue)

        try:
            logger.info(
                "VLA 调试相机订阅下发 | machine_id={} subscription_id={} fps={}",
                normalized_machine_id,
                subscription_id,
                _normalize_camera_fps(fps),
            )
            await self._connection_registry.send_vla_debug_camera_subscription(
                normalized_machine_id,
                subscription_id=subscription_id,
                fps=_normalize_camera_fps(fps),
                subscribe=True,
            )
        except Exception:
            async with self._lock:
                subscribers = self._camera_subscribers.get(normalized_machine_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._camera_subscribers.pop(normalized_machine_id, None)
            raise

        latest = self._latest_camera_frames.get(normalized_machine_id)
        if latest is not None:
            yield latest

        try:
            while True:
                frame = await queue.get()
                logger.info(
                    "VLA 调试相机帧推送 SSE | machine_id={} subscription_id={}",
                    normalized_machine_id,
                    frame.get("subscription_id"),
                )
                yield frame
        finally:
            should_unsubscribe = False
            async with self._lock:
                subscribers = self._camera_subscribers.get(normalized_machine_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._camera_subscribers.pop(normalized_machine_id, None)
                        should_unsubscribe = True
            if should_unsubscribe:
                with contextlib.suppress(Exception):
                    logger.info(
                        (
                            "VLA 调试相机订阅取消 | machine_id={} "
                            "subscription_id={}"
                        ),
                        normalized_machine_id,
                        subscription_id,
                    )
                    await self._connection_registry.send_vla_debug_camera_subscription(
                        normalized_machine_id,
                        subscription_id=subscription_id,
                        fps=_normalize_camera_fps(fps),
                        subscribe=False,
                    )

    async def handle_edge_tool_finished(
        self,
        *,
        invocation_id: str,
        ok: bool,
    ) -> None:
        normalized_invocation_id = _require_text(invocation_id, "invocation_id")
        async with self._lock:
            for task in list(self._active_tasks_by_machine.values()):
                if task.invocation_id != normalized_invocation_id:
                    continue
                task.status = "completed" if ok else "failed"
                task.updated_at = _now()
                self._active_tasks_by_machine.pop(task.machine_id, None)
                return

    async def _stop_active_task(self, task: ActiveVLATask) -> None:
        task.status = "stopping"
        task.updated_at = _now()
        if task.invocation_id:
            await self._connection_registry.cancel_edge_tool(
                task.machine_id,
                invocation_id=task.invocation_id,
                tool_name=EDGE_TOOL_VLA_CONTROL,
            )
        else:
            await self._connection_registry.invoke_edge_tool(
                task.machine_id,
                tool_name=EDGE_TOOL_VLA_CONTROL,
                session_id=None,
                timeout_ms=STOP_TOOL_TIMEOUT_MS,
                arguments={"command": "stop", "reason": "vla_debug_stop"},
            )
        task.status = "stopped"
        task.updated_at = _now()
        self._active_tasks_by_machine.pop(task.machine_id, None)

    async def _validate_machine(self, machine_id: str, robot_type: str) -> None:
        machine = await self._find_machine(machine_id)
        if machine is None:
            raise VLADebugError("machine not found", code="machine_not_found")
        if _normalize_robot_type(machine.get("type_name")) != robot_type:
            raise VLADebugError("machine robot_type mismatch", code="robot_type_mismatch")

    async def _find_machine(self, machine_id: str) -> dict[str, object] | None:
        for machine in await self._list_machines():
            if machine.get("machine_id") == machine_id:
                return machine
        return None

    async def _find_machine_or_raise(self, machine_id: str) -> dict[str, object]:
        machine = await self._find_machine(machine_id)
        if machine is None:
            raise VLADebugError("machine not found", code="machine_not_found")
        return machine

    async def _list_machines(self) -> list[dict[str, object]]:
        return await self._orchestration_service.list_machines()

    async def _get_policy_service(
        self, policy_service_id: str
    ) -> VLAPolicyServiceRecord | None:
        if self._store is not None:
            await self.start()
            return await self._store.get_policy_service(policy_service_id)
        return self._policy_services.get(policy_service_id)

    def _find_task(self, task_id: str) -> ActiveVLATask:
        for task in self._active_tasks_by_machine.values():
            if task.task_id == task_id:
                return task
        raise VLADebugError("active VLA task not found", code="task_not_found")

    def _build_live_camera_response(
        self,
        machine_id: str,
        *,
        machine: dict[str, object],
    ) -> dict[str, object]:
        machine_online = bool(machine.get("is_online"))
        status = (
            self._live_camera_status_by_machine.get(machine_id)
            if machine_online
            else None
        )
        normalized_status = status or {"state": LIVE_CAMERA_STATE_STOPPED}
        state = self._current_live_camera_state(machine_id)
        if not machine_online:
            state = LIVE_CAMERA_STATE_STOPPED
        quality = normalized_status.get("quality") if isinstance(normalized_status, dict) else None
        if not isinstance(quality, dict):
            quality = {"width": 640, "height": 480, "fps": 15, "bitrate_bps": 1_000_000}
        rtmp_targets = (
            _normalize_rtmp_targets(normalized_status.get("rtmp_targets"))
            if state == LIVE_CAMERA_STATE_PUBLISHING
            else []
        )
        return {
            "machine_id": machine_id,
            "machine": machine,
            "state": state,
            "status": normalized_status,
            "rtmp_targets": rtmp_targets,
            "playback": [
                _build_srs_playback(target, config=self._live_camera_config)
                for target in rtmp_targets
            ],
            "quality": quality,
            "srs": {
                "app": self._live_camera_config.srs_app,
                "webrtc_base_url": self._live_camera_config.srs_webrtc_base_url,
                "http_base_url": self._live_camera_config.srs_http_base_url,
            },
        }

    def _current_live_camera_state(self, machine_id: str) -> str:
        status = self._live_camera_status_by_machine.get(machine_id)
        if status is None:
            return LIVE_CAMERA_STATE_STOPPED
        return _normalize_live_camera_state(status.get("state"))

    def _mark_memory_policy_service_used(
        self, record: VLAPolicyServiceRecord
    ) -> None:
        now = _now()
        self._policy_services[record.service_id] = VLAPolicyServiceRecord(
            service_id=record.service_id,
            robot_type=record.robot_type,
            name=record.name,
            endpoint=record.endpoint,
            protocol=record.protocol,
            created_at=record.created_at,
            updated_at=now,
            last_used_at=now,
        )

    async def _remember_instruction(
        self,
        *,
        robot_type: str,
        instruction: str,
        machine_id: str,
        policy_service_id: str,
    ) -> None:
        if self._store is not None:
            await self.start()
            await self._store.remember_instruction(
                robot_type=robot_type,
                instruction=instruction,
                machine_id=machine_id,
                policy_service_id=policy_service_id,
            )
            return
        key = f"{robot_type}:{instruction}"
        current = self._instruction_history.get(key)
        now = _now().isoformat()
        self._instruction_history[key] = {
            "instruction_text": instruction,
            "robot_type": robot_type,
            "last_used_at": now,
            "use_count": int((current or {}).get("use_count") or 0) + 1,
            "last_machine_id": machine_id,
            "last_policy_service_id": policy_service_id,
        }


def _serialize_policy_service(record: VLAPolicyServiceRecord) -> dict[str, object]:
    return {
        "service_id": record.service_id,
        "robot_type": record.robot_type,
        "name": record.name,
        "endpoint": record.endpoint,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
    }


def _serialize_task(task: ActiveVLATask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "machine_id": task.machine_id,
        "robot_type": task.robot_type,
        "policy_service_id": task.policy_service_id,
        "instruction": task.instruction,
        "execution_space": task.execution_space,
        "execution_mode": task.execution_mode,
        "invocation_id": task.invocation_id,
        "status": task.status,
        "started_at": task.started_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _normalize_robot_type(value: object) -> str | None:
    text = _optional_text(value)
    return text.upper() if text else None


def _require_robot_type(value: str) -> str:
    robot_type = _normalize_robot_type(value)
    if robot_type is None:
        raise VLADebugError("robot_type is required", code="invalid_robot_type")
    return robot_type


def _require_text(value: object, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise VLADebugError(f"{field_name} is required", code=f"invalid_{field_name}")
    return text


def _normalize_execution_space(value: str) -> str:
    text = _require_text(value, "execution_space").lower()
    if text not in SUPPORTED_EXECUTION_SPACES:
        raise VLADebugError("unsupported execution_space", code="unsupported_execution_space")
    return text


def _normalize_execution_mode(value: str) -> str:
    text = _require_text(value, "execution_mode").lower()
    if text not in EDGE_EXECUTION_MODE_BY_API_MODE:
        raise VLADebugError("unsupported execution_mode", code="unsupported_execution_mode")
    return text


def _normalize_camera_fps(value: float) -> float:
    if value <= 0:
        return DEFAULT_CAMERA_FPS
    return min(float(value), MAX_CAMERA_FPS)


def _normalize_live_camera_state(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in LIVE_CAMERA_STATES:
        return text
    return LIVE_CAMERA_STATE_STOPPED


def _build_live_camera_config(config: dict[str, Any] | None) -> LiveCameraConfig:
    if not isinstance(config, dict):
        return LiveCameraConfig()
    return LiveCameraConfig(
        srs_webrtc_base_url=(
            _optional_text(config.get("srs_webrtc_base_url"))
            or DEFAULT_SRS_WEBRTC_BASE_URL
        ),
        srs_http_base_url=(
            _optional_text(config.get("srs_http_base_url"))
            or DEFAULT_SRS_HTTP_BASE_URL
        ),
        srs_app=_optional_text(config.get("srs_app")) or DEFAULT_SRS_APP,
    )


def _normalize_rtmp_targets(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    targets: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        view = _optional_text(item.get("view"))
        url = _optional_text(item.get("url"))
        if view is None or url is None:
            continue
        targets.append({"view": view, "url": url})
    return targets


def _build_srs_playback(
    target: dict[str, str],
    *,
    config: LiveCameraConfig,
) -> dict[str, str]:
    stream = _stream_name_from_rtmp_url(target["url"])
    encoded_app = quote(config.srs_app, safe="")
    encoded_stream = quote(stream, safe="")
    return {
        "view": target["view"],
        "stream": stream,
        "rtmp_url": target["url"],
        "whep_url": (
            f"{config.srs_webrtc_base_url.rstrip('/')}/rtc/v1/whep/"
            f"?app={encoded_app}&stream={encoded_stream}"
        ),
        "flv_url": (
            f"{config.srs_http_base_url.rstrip('/')}/"
            f"{config.srs_app.strip('/')}/{stream}.flv"
        ),
    }


def _stream_name_from_rtmp_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["VLADebugError", "VLADebugService", "VLAPolicyServiceRecord"]
