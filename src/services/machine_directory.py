"""外部 machine 目录查询。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_MACHINE_DIRECTORY_SOURCE = "nexus-api"
DEFAULT_MACHINE_DIRECTORY_TIMEOUT_SEC = 30.0


@dataclass(frozen=True, slots=True)
class MachineRecord:
    """外部设备目录中的单台设备。"""

    machine_id: str
    name: str
    description: str
    is_online: bool
    is_active: bool = True
    last_online: int | None = None
    type_name: str | None = None


class MachineDirectoryClient:
    """查询外部设备目录。"""

    def __init__(
        self,
        *,
        endpoint: str | None,
        service_key: str | None,
        request_source: str = DEFAULT_MACHINE_DIRECTORY_SOURCE,
        timeout_sec: float = DEFAULT_MACHINE_DIRECTORY_TIMEOUT_SEC,
    ) -> None:
        self._endpoint = endpoint.strip() if endpoint else None
        self._service_key = service_key.strip() if service_key else None
        self._request_source = request_source
        self._timeout_sec = timeout_sec

    async def list_machines(self) -> list[MachineRecord]:
        if not self._endpoint:
            return []

        timeout = httpx.Timeout(self._timeout_sec, connect=5.0)
        headers = {"X-Request-Source": self._request_source}
        if self._service_key:
            headers["X-Internal-Service-Key"] = self._service_key

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self._endpoint, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return []

        if not _is_success_payload(payload):
            return []
        return _extract_machine_records(payload)

    async def get_machine(self, machine_id: str) -> MachineRecord | None:
        normalized_machine_id = machine_id.strip()
        if not normalized_machine_id:
            return None
        items = await self.list_machines()
        for item in items:
            if item.machine_id == normalized_machine_id:
                return item
        return None


def _extract_machine_records(payload: object) -> list[MachineRecord]:
    candidates = _find_candidate_list(payload)
    results: list[MachineRecord] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        activation = item.get("activation")
        if activation is not None and activation is not True:
            continue
        machine_id = (
            _normalize_optional_text(item.get("machine_id"))
            or _normalize_optional_text(item.get("robot_id"))
            or _normalize_optional_text(item.get("device_id"))
            or _normalize_optional_text(item.get("uid"))
            or _normalize_optional_text(item.get("id"))
        )
        name = (
            _normalize_optional_text(item.get("name"))
            or _normalize_optional_text(item.get("robot_name"))
            or _normalize_optional_text(item.get("title"))
        )
        if not machine_id or not name:
            continue
        results.append(
            MachineRecord(
                machine_id=machine_id,
                name=name,
                description=str(
                    item.get("description")
                    or item.get("address")
                    or item.get("area")
                    or item.get("scene")
                    or ""
                ).strip(),
                is_online=_coerce_online_status(item.get("status")),
                is_active=True,
                last_online=_coerce_optional_int(item.get("last_online")),
                type_name=_normalize_optional_text(item.get("type_name")),
            )
        )
    return results


def _find_candidate_list(payload: object) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "list", "machines", "robots", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _find_candidate_list(value)
            if nested:
                return nested
    return []


def _is_success_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    code = payload.get("code")
    if code is None:
        return True
    return str(code).strip() == "200"


def _coerce_online_status(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "online", "ready", "idle", "running"}


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(float(stripped))
            except ValueError:
                return None
    return None


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["MachineDirectoryClient", "MachineRecord"]
