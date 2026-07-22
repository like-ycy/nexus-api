"""Nexus API 启动入口。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.config import load_config
from src.core.http_server import HttpApiServer
from src.core.websocket_server import WebSocketServer
from src.core.enums import DebugLevel
from src.core.exceptions import NexusApiError
from src.utils.logging import setup_logger

__all__ = ["NexusApplication", "main", "main_async"]


class NexusApplication:
    """应用组装与生命周期管理。"""

    def __init__(
        self,
        *,
        websocket_server: WebSocketServer,
        http_server: HttpApiServer,
    ) -> None:
        self._websocket_server = websocket_server
        self._http_server = http_server

    @classmethod
    def build(cls, config: dict[str, object]) -> "NexusApplication":
        http_server = HttpApiServer(config)
        websocket_server = WebSocketServer(
            config,
            conversation_recorder=http_server.conversation_recorder,
            orchestration_service=getattr(
                http_server.app.state,
                "orchestration_service",
                None,
            ),
            vla_debug_service=getattr(
                http_server.app.state,
                "vla_debug_service",
                None,
            ),
        )
        return cls(
            websocket_server=websocket_server,
            http_server=http_server,
        )

    async def serve(self) -> None:
        websocket_task: asyncio.Task[None] | None = None
        http_task: asyncio.Task[None] | None = None
        try:
            websocket_task = asyncio.create_task(
                self._websocket_server.start(),
                name="nexus-websocket-server",
            )
            http_task = asyncio.create_task(
                self._http_server.start(),
                name="nexus-http-server",
            )
            await asyncio.gather(websocket_task, http_task)
        finally:
            for task in (websocket_task, http_task):
                if task is not None and not task.done():
                    task.cancel()
            if websocket_task is not None or http_task is not None:
                await asyncio.gather(
                    *(task for task in (websocket_task, http_task) if task is not None),
                    return_exceptions=True,
                )
            await self._http_server.close()


async def main_async() -> None:
    config = load_config()
    logging_config = config.get("logging") or {}
    level = str(logging_config.get("level") or "INFO").upper()
    log_path = logging_config.get("log_path")
    setup_logger(
        level=DebugLevel(level).value,
        log_path=log_path if isinstance(log_path, Path) else None,
    )
    app = NexusApplication.build(config)
    await app.serve()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        return
    except NexusApiError as exc:
        raise SystemExit(f"nexus-api 启动失败，详情：{str(exc)}")


if __name__ == "__main__":
    main()
