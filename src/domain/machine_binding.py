"""设备绑定领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MachineBindingRecord:
    """单台设备当前的云端绑定结果。"""

    machine_id: str
    orchestration_id: str | None
    binding_version: int
    created_at: datetime
    updated_at: datetime


__all__ = ["MachineBindingRecord"]
