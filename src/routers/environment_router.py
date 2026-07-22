"""环境管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.services.environment import (
    EnvironmentNotFoundError,
    EnvironmentPointNotFoundError,
    EnvironmentService,
)
from src.utils.response import success

router = APIRouter(prefix="/api/environments", tags=["environments"])


class EnvironmentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="环境名称")
    description: str = Field(default="", max_length=2000, description="环境描述")


class EnvironmentPointRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="点位名称")
    tag: str = Field(..., min_length=1, max_length=100, description="底盘点位标识")
    description: str = Field(default="", max_length=2000, description="点位描述")
    sort_order: int = Field(default=0, description="排序")


def get_environment_service(request: Request) -> EnvironmentService:
    service = getattr(request.app.state, "environment_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="environment service is disabled")
    return service


@router.get("", summary="查询环境列表")
async def list_environments(
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    items = await service.list_environments()
    return success(data={"items": items, "total": len(items)})


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建环境")
async def create_environment(
    request: EnvironmentRequest,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    return success(
        data=await service.create_environment(
            name=request.name,
            description=request.description,
        )
    )


@router.get("/{environment_id}", summary="查询环境详情")
async def get_environment(
    environment_id: str,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(data=await service.get_environment(environment_id))
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc


@router.put("/{environment_id}", summary="修改环境")
async def update_environment(
    environment_id: str,
    request: EnvironmentRequest,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.update_environment(
                environment_id,
                name=request.name,
                description=request.description,
            )
        )
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc


@router.delete("/{environment_id}", summary="删除环境")
async def delete_environment(
    environment_id: str,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(data=await service.delete_environment(environment_id))
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc


@router.post(
    "/{environment_id}/points",
    status_code=status.HTTP_201_CREATED,
    summary="创建环境点位",
)
async def create_environment_point(
    environment_id: str,
    request: EnvironmentPointRequest,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.create_environment_point(
                environment_id,
                name=request.name,
                tag=request.tag,
                description=request.description,
                sort_order=request.sort_order,
            )
        )
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc


@router.put("/{environment_id}/points/{point_id}", summary="修改环境点位")
async def update_environment_point(
    environment_id: str,
    point_id: str,
    request: EnvironmentPointRequest,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.update_environment_point(
                environment_id,
                point_id,
                name=request.name,
                tag=request.tag,
                description=request.description,
                sort_order=request.sort_order,
            )
        )
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc
    except EnvironmentPointNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="environment point not found"
        ) from exc


@router.delete("/{environment_id}/points/{point_id}", summary="删除环境点位")
async def delete_environment_point(
    environment_id: str,
    point_id: str,
    service: EnvironmentService = Depends(get_environment_service),
) -> dict[str, object]:
    try:
        return success(
            data=await service.delete_environment_point(environment_id, point_id)
        )
    except EnvironmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="environment not found") from exc
    except EnvironmentPointNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="environment point not found"
        ) from exc


__all__ = ["get_environment_service", "router"]
