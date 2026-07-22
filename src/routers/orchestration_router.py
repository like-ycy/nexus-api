"""编排应用配置路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from src.services.orchestration import OrchestrationNotFoundError, OrchestrationService
from src.utils.response import success

router = APIRouter(prefix="/api/orchestrations", tags=["orchestrations"])


class CreateOrchestrationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="应用名称")
    description: str = Field(default="", max_length=2000, description="应用描述")


class SaveOrchestrationConfigRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="应用名称")
    description: str = Field(default="", max_length=2000, description="应用描述")
    prompt: str = Field(default="", max_length=12000, description="Prompt 提示词")
    robot_id: str | None = Field(default=None, description="绑定机器人 ID")
    knowledge_base_id: str | None = Field(default=None, description="知识库 ID")
    environment_id: str | None = Field(default=None, description="环境 ID")
    voice_id: str | None = Field(default=None, description="语音 ID")
    skill_ids: list[str] = Field(default_factory=list, description="技能 ID 列表")
    welcome_message: str = Field(default="", max_length=2000, description="欢迎语")


def get_orchestration_service(request: Request) -> OrchestrationService:
    service = getattr(request.app.state, "orchestration_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="orchestration service is disabled")
    return service


@router.get("", summary="查询编排应用列表")
async def list_orchestrations(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=10, ge=1, le=200, description="每页数量"),
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    return success(
        data=await service.list_orchestrations(page=page, page_size=page_size)
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建编排应用")
async def create_orchestration(
    request: CreateOrchestrationRequest,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    payload = await service.create_orchestration(
        name=request.name, description=request.description
    )
    return success(data=payload)


@router.get("/{orchestration_id}/config", summary="查询编排应用配置")
async def get_orchestration_config(
    orchestration_id: str,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    try:
        return success(data=await service.get_orchestration_config(orchestration_id))
    except OrchestrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="orchestration not found") from exc


@router.put("/{orchestration_id}/config", summary="保存编排应用配置")
async def save_orchestration_config(
    orchestration_id: str,
    request: SaveOrchestrationConfigRequest,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.save_orchestration_config(
                orchestration_id,
                name=request.name,
                description=request.description,
                prompt=request.prompt,
                robot_id=request.robot_id,
                knowledge_base_id=request.knowledge_base_id,
                environment_id=request.environment_id,
                voice_id=request.voice_id,
                skill_ids=request.skill_ids,
                welcome_message=request.welcome_message,
            )
        )
    except OrchestrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="orchestration not found") from exc


@router.delete("/{orchestration_id}", summary="删除编排应用")
async def delete_orchestration(
    orchestration_id: str,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    try:
        return success(data=await service.delete_orchestration(orchestration_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OrchestrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="orchestration not found") from exc


__all__ = ["get_orchestration_service", "router"]
