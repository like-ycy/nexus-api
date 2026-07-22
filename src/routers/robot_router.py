"""机器人列表与技能展示路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.routers.orchestration_router import get_orchestration_service
from src.services.orchestration import OrchestrationService, RobotNotFoundError
from src.utils.response import success

router = APIRouter(prefix="/api/robots", tags=["robots"])


@router.get("", summary="查询机器人列表")
async def list_robots(
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    return success(data=await service.list_robots())


@router.get("/{robot_id}/skills", summary="查询机器人技能列表")
async def list_robot_skills(
    robot_id: str,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    try:
        return success(data=await service.list_robot_skills(robot_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RobotNotFoundError as exc:
        raise HTTPException(status_code=404, detail="robot not found") from exc


__all__ = ["router"]
