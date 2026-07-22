"""通用 HTTP API 服务。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.domain.session import ConversationRecorder
from src.routers import register_http_routers
from src.db.vla_debug_store import PostgresVLADebugStore, VLADebugStore
from src.services.conversation import ConversationService, build_conversation_service
from src.services.environment import EnvironmentService
from src.services.knowledge_base import (
    KnowledgeBaseService,
    build_knowledge_base_service,
)
from src.services.orchestration import (
    OrchestrationService,
    build_orchestration_service,
)
from src.services.vla_debug import VLADebugService
from src.utils.logging import logger
from src.utils.response import Code, error


class _EmbeddedUvicornServer:
    """嵌入式 uvicorn 包装，禁用内部信号捕获，交由外层应用统一处理 Ctrl+C。"""

    def __init__(self, server: Any) -> None:
        self._server = server

    @property
    def should_exit(self) -> bool:
        return bool(self._server.should_exit)

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._server.should_exit = value

    async def serve(self, sockets: Sequence[Any] | None = None) -> None:
        await self._server._serve(sockets)


class HttpApiServer:
    """基于 FastAPI 的通用 HTTP 服务。"""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        conversation_service: ConversationService | None = None,
        knowledge_base_service: KnowledgeBaseService | None = None,
        environment_service: EnvironmentService | None = None,
        orchestration_service: OrchestrationService | None = None,
        vla_debug_service: VLADebugService | None = None,
    ) -> None:
        http_config = _require_section(config, "http")
        self._host = str(http_config.get("host") or "0.0.0.0")
        self._port = int(http_config.get("port") or 8080)
        self._uvicorn_server: Any | None = None
        self._conversation_service = (
            conversation_service
            if conversation_service is not None
            else _build_conversation_service(config)
        )
        self._knowledge_base_service = (
            knowledge_base_service
            if knowledge_base_service is not None
            else _build_knowledge_base_service(config)
        )
        self.app = FastAPI(
            title="Nexus API HTTP",
            description="Nexus API 管理与查询接口",
            version="0.1.0",
        )
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._register_exception_handlers()
        self.app.state.config = config
        self.app.state.conversation_service = self._conversation_service
        self.app.state.knowledge_base_service = self._knowledge_base_service
        resolved_environment_service = (
            environment_service
            if environment_service is not None
            else _build_environment_service(config)
        )
        self.app.state.environment_service = resolved_environment_service
        resolved_orchestration_service = build_orchestration_service(
            config,
            conversation_service=self._conversation_service,
            knowledge_base_service=self._knowledge_base_service,
        )
        if orchestration_service is not None:
            resolved_orchestration_service = orchestration_service
        self.app.state.orchestration_service = resolved_orchestration_service
        resolved_vla_debug_service = vla_debug_service
        if (
            resolved_vla_debug_service is None
            and resolved_orchestration_service is not None
        ):
            resolved_vla_debug_service = VLADebugService(
                orchestration_service=resolved_orchestration_service,
                connection_registry=resolved_orchestration_service.connection_registry,
                store=_build_vla_debug_store(config),
                live_camera_config=config.get("live_camera")
                if isinstance(config.get("live_camera"), dict)
                else None,
            )
        self.app.state.vla_debug_service = resolved_vla_debug_service
        self._route_count = register_http_routers(self.app)

    @property
    def conversation_recorder(self) -> ConversationRecorder | None:
        return self._conversation_service

    @property
    def route_count(self) -> int:
        return self._route_count

    async def start(self) -> None:
        if self._uvicorn_server is not None:
            return

        if self._conversation_service is not None:
            await self._conversation_service.start()

        try:
            import uvicorn
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 uvicorn，无法启动 HTTP API。请先执行 uv sync。"
            ) from exc

        config = uvicorn.Config(
            self.app,
            host=self._host,
            port=self._port,
            log_level="info",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = _EmbeddedUvicornServer(server)
        logger.info(
            "HTTP API 已就绪: http://{}:{} | routes={}",
            self._host,
            self._port,
            self.route_count,
        )
        await self._uvicorn_server.serve()

    async def close(self) -> None:
        server = self._uvicorn_server
        self._uvicorn_server = None
        if server is not None:
            server.should_exit = True
        if self._conversation_service is not None:
            await self._conversation_service.close()
        if self._knowledge_base_service is not None:
            await self._knowledge_base_service.close()
        environment_service = getattr(self.app.state, "environment_service", None)
        if environment_service is not None:
            await environment_service.close()
        orchestration_service = getattr(self.app.state, "orchestration_service", None)
        if orchestration_service is not None:
            await orchestration_service.close()
        vla_debug_service = getattr(self.app.state, "vla_debug_service", None)
        close_vla_debug = getattr(vla_debug_service, "close", None)
        if close_vla_debug is not None:
            await close_vla_debug()

    def _register_exception_handlers(self) -> None:
        @self.app.exception_handler(HTTPException)
        async def _http_exception_handler(
            request: Request,
            exc: HTTPException,
        ) -> JSONResponse:
            detail = exc.detail
            if isinstance(detail, dict):
                message = str(detail.get("msg") or detail.get("message") or "error")
                payload = error(code=exc.status_code, msg=message, data=detail)
            else:
                payload = error(code=exc.status_code, msg=str(detail or "error"))
            return JSONResponse(status_code=exc.status_code, content=payload)

        @self.app.exception_handler(RequestValidationError)
        async def _validation_exception_handler(
            request: Request,
            exc: RequestValidationError,
        ) -> JSONResponse:
            payload = error(
                code=Code.VALIDATION_ERROR,
                msg="validation error",
                data=jsonable_encoder(exc.errors()),
            )
            return JSONResponse(status_code=422, content=payload)

        @self.app.exception_handler(Exception)
        async def _unexpected_exception_handler(
            request: Request,
            exc: Exception,
        ) -> JSONResponse:
            logger.exception("HTTP 未处理异常 | path={}", request.url.path)
            payload = error(code=Code.INTERNAL_ERROR, msg="internal server error")
            return JSONResponse(status_code=500, content=payload)


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


def _build_conversation_service(config: dict[str, Any]) -> ConversationService | None:
    conversation_config = config.get("conversation")
    if not isinstance(conversation_config, dict):
        return None
    return build_conversation_service(config)


def _build_knowledge_base_service(
    config: dict[str, Any],
) -> KnowledgeBaseService | None:
    return build_knowledge_base_service(config)


def _build_environment_service(config: dict[str, Any]) -> EnvironmentService | None:
    database_config = config.get("database")
    if not isinstance(database_config, dict):
        return None
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None
    return EnvironmentService(config)


def _build_vla_debug_store(config: dict[str, Any]) -> VLADebugStore | None:
    database_config = config.get("database")
    if not isinstance(database_config, dict):
        return None
    dsn = str(database_config.get("dsn") or "").strip()
    if not dsn:
        return None
    return PostgresVLADebugStore(
        dsn=dsn,
        min_pool_size=int(database_config.get("min_pool_size") or 1),
        max_pool_size=int(database_config.get("max_pool_size") or 5),
        command_timeout_sec=float(database_config.get("command_timeout_sec") or 10.0),
    )


__all__ = ["HttpApiServer"]
