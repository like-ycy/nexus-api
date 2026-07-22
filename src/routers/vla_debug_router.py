"""VLA model debug routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.services.vla_debug import VLADebugError, VLADebugService
from src.utils.response import success

router = APIRouter(prefix="/api/vla-debug", tags=["vla-debug"])


class SavePolicyServiceRequest(BaseModel):
    service_id: str | None = None
    name: str = Field(..., min_length=1)
    endpoint: str = Field(..., min_length=1)


class StartVLATaskRequest(BaseModel):
    robot_type: str = Field(..., min_length=1)
    machine_id: str = Field(..., min_length=1)
    policy_service_id: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1)
    execution_space: str = "joint"
    execution_mode: str = "sync"


def get_vla_debug_service(request: Request) -> VLADebugService:
    service = getattr(request.app.state, "vla_debug_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="vla debug service is disabled")
    return service


@router.get("/robot-types", summary="查询可调试机器人类型")
async def list_robot_types(
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    return success(data={"items": await service.list_robot_types()})


@router.get("/robot-types/{robot_type}/machines", summary="查询类型下机器")
async def list_machines(
    robot_type: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data={"items": await service.list_machines(robot_type)})
    except VLADebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/machines", summary="查询全部可调试机器")
async def list_all_machines(
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    return success(data={"items": await service.list_all_machines()})


@router.get("/robot-types/{robot_type}/policy-services", summary="查询 VLA 服务")
async def list_policy_services(
    robot_type: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data={"items": await service.list_policy_services(robot_type)})
    except VLADebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/robot-types/{robot_type}/policy-services", summary="保存 VLA 服务")
async def save_policy_service(
    robot_type: str,
    request: SavePolicyServiceRequest,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.save_policy_service(
                robot_type=robot_type,
                service_id=request.service_id,
                name=request.name,
                endpoint=request.endpoint,
            )
        )
    except VLADebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/instructions", summary="查询 VLA instruction 输入历史")
async def list_instruction_history(
    robot_type: str = Query(..., min_length=1),
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data={"items": await service.list_instruction_history(robot_type)})
    except VLADebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks", summary="启动 VLA 调试任务")
async def start_task(
    request: StartVLATaskRequest,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.start_task(
                robot_type=request.robot_type,
                machine_id=request.machine_id,
                policy_service_id=request.policy_service_id,
                instruction=request.instruction,
                execution_space=request.execution_space,
                execution_mode=request.execution_mode,
            )
        )
    except VLADebugError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/stop", summary="停止 VLA 调试任务")
async def stop_task(
    task_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.stop_task(task_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/machines/{machine_id}/reset", summary="复位机器 VLA runtime")
async def reset_machine(
    machine_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.reset_machine(machine_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/machines/{machine_id}/state", summary="查询机器 VLA 调试状态")
async def get_machine_state(
    machine_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.get_machine_state(machine_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/machines/{machine_id}/live-camera", summary="查询机器实时相机预览")
async def get_machine_live_camera(
    machine_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.get_live_camera(machine_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/machines/{machine_id}/live-camera/start", summary="开启机器实时相机预览")
async def start_machine_live_camera(
    machine_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.start_live_camera(machine_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/machines/{machine_id}/live-camera/stop", summary="关闭机器实时相机预览")
async def stop_machine_live_camera(
    machine_id: str,
    service: VLADebugService = Depends(get_vla_debug_service),
) -> dict[str, object]:
    try:
        return success(data=await service.stop_live_camera(machine_id))
    except VLADebugError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["get_vla_debug_service", "router"]
