"""设备管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.routers.orchestration_router import get_orchestration_service
from src.services.orchestration import (
    MachineNotFoundError,
    OrchestrationNotFoundError,
    OrchestrationService,
)
from src.utils.response import success

router = APIRouter(prefix="/api/machines", tags=["machines"])


class SaveMachineBindingRequest(BaseModel):
    binding_orchestration_id: str | None = Field(
        default=None,
        description="当前机器绑定的编排 ID",
    )

    def resolved_orchestration_id(self) -> str | None:
        return self.binding_orchestration_id


@router.get("", summary="查询设备列表及绑定状态")
async def list_machines(
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    items = await service.list_machines()
    return success(data={"items": items, "total": len(items)})


@router.put("/{machine_id}", summary="保存单台设备绑定")
async def save_machine(
    machine_id: str,
    request: SaveMachineBindingRequest,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.save_machine_binding(
                machine_id,
                orchestration_id=request.resolved_orchestration_id(),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MachineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="machine not found") from exc
    except OrchestrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="orchestration not found") from exc


__all__ = ["router"]
