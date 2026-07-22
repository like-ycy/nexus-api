"""通用 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["common"])


@router.get("/health", summary="健康检查")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", summary="服务根路径")
async def root() -> dict[str, str]:
    return {"message": "Nexus API is running"}


__all__ = ["router"]
