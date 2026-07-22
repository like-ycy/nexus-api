"""HTTP 路由总装配。"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from src.routers.common_router import router as common_router
from src.routers.conversation_router import router as conversation_router
from src.routers.environment_router import router as environment_router
from src.routers.knowledge_base_router import router as knowledge_base_router
from src.routers.machine_router import router as machine_router
from src.routers.orchestration_router import router as orchestration_router
from src.routers.robot_router import router as robot_router
from src.routers.vla_debug_router import router as vla_debug_router
from src.routers.voice_router import router as voice_router

HTTP_ROUTERS: tuple[APIRouter, ...] = (
    common_router,
    environment_router,
    machine_router,
    robot_router,
    voice_router,
    vla_debug_router,
    orchestration_router,
    knowledge_base_router,
    conversation_router,
)


def register_http_routers(app: FastAPI) -> int:
    route_count = 0
    for router in HTTP_ROUTERS:
        app.include_router(router)
        route_count += len(router.routes)
    return route_count


__all__ = ["HTTP_ROUTERS", "register_http_routers"]
