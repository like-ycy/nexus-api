"""WebSocket 服务启动与连接分发。"""

from __future__ import annotations

import asyncio
from typing import Any

import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from src.core.connection_handler import ConnectionHandler
from src.domain.session import ConversationRecorder
from src.providers import initialize_modules
from src.services.orchestration import (
    OrchestrationService,
    build_orchestration_service,
)
from src.services.vla_debug import VLADebugService
from src.utils.logging import logger

WEBSOCKET_MAX_MESSAGE_SIZE_BYTES = 10 * 1024 * 1024


class WebSocketServer:
    """WebSocket 服务器：负责监听、接入和连接分发。"""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        conversation_recorder: ConversationRecorder | None = None,
        orchestration_service: OrchestrationService | None = None,
        vla_debug_service: VLADebugService | None = None,
    ) -> None:
        self.config = config

        # 初始化 VAD,ASR,LLM
        self.modules = initialize_modules(
            self.config,
            init_vad=True,
            init_asr=True,
            init_llm=True,
            init_tts=True,
        )
        self.conversation_recorder = conversation_recorder
        self.orchestration_service = (
            orchestration_service
            if orchestration_service is not None
            else build_orchestration_service(config)
        )
        self.vla_debug_service = vla_debug_service

    async def start(self) -> None:
        websocket_config = self.config["websocket"]
        host = str(websocket_config.get("host") or "0.0.0.0")
        port = int(websocket_config.get("port") or 8765)

        server = await serve(
            self._handle_connection,
            host,
            port,
            max_size=WEBSOCKET_MAX_MESSAGE_SIZE_BYTES,
            compression=None,
        )
        try:
            logger.info(
                "Nexus API 启动成功: ws://{}:{} | VAD={} ASR={} LLM={} TTS={}",
                host,
                port,
                self.modules.vad_model_path.name
                if self.modules.vad_model_path
                else None,
                self.modules.asr_provider_name,
                self.modules.llm_provider_name,
                self.modules.tts_provider_name,
            )
            await asyncio.Future()
        finally:
            server.close()
            await server.wait_closed()
            logger.info("Nexus API 服务已停止")

    async def _handle_connection(self, websocket: websockets.ServerConnection) -> None:
        client = f"{websocket.remote_address}"
        handler = ConnectionHandler(
            self.config,
            self.modules,
            conversation_recorder=self.conversation_recorder,
            orchestration_service=self.orchestration_service,
            vla_debug_service=self.vla_debug_service,
        )
        logger.info("客户端已连接: {}", client)
        try:
            await handler.handle_connection(websocket)
            logger.info("客户端连接处理结束: {}", client)
        except ConnectionClosed as exc:
            logger.info("客户端连接已关闭 | client={} detail={}", client, exc)
        except Exception:
            logger.exception("处理连接出错 | client={}", client)
            try:
                if getattr(websocket, "state", None) is not None:
                    if websocket.state.name != "CLOSED":
                        await websocket.close(code=1011, reason="internal_error")
                else:
                    await websocket.close(code=1011, reason="internal_error")
            except Exception as close_error:
                logger.error(
                    "服务器端异常关闭连接时出错 | client={} err={}", client, close_error
                )
