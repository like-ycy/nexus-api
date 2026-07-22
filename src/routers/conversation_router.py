"""对话与历史路由注册。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.domain import ConversationRecord, ConversationTurnRecord
from src.services.conversation import ConversationService
from src.services.orchestration import (
    LlmServiceUnavailableError,
    OrchestrationNotFoundError,
    OrchestrationService,
    encode_sse_event,
)
from src.utils.response import success

router = APIRouter(tags=["conversations"])


class OrchestrationConversationRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000, description="用户问题")
    response_mode: Literal["text", "audio"] = Field(
        default="text", description="返回文本或语音"
    )
    session_id: str | None = Field(default=None, description="会话 ID，用于连续对话")
    streaming: bool = Field(default=False, description="是否使用 SSE 流式返回")


def get_conversation_service(request: Request) -> ConversationService:
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is None:
        raise HTTPException(status_code=503, detail="conversation service is disabled")
    return conversation_service


def get_orchestration_service(request: Request) -> OrchestrationService:
    service = getattr(request.app.state, "orchestration_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="orchestration service is disabled")
    return service


@router.get(
    "/api/orchestrations/{orchestration_id}/conversations",
    summary="查询编排应用对话历史",
)
async def list_orchestration_conversations(
    orchestration_id: str,
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int | None = Query(default=None, ge=1, description="每页数量"),
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> dict[str, object]:
    config = getattr(request.app.state, "config", {})
    conversation_config = config.get("conversation") if isinstance(config, dict) else {}
    if not isinstance(conversation_config, dict):
        conversation_config = {}
    default_session_limit = int(conversation_config.get("session_limit") or 20)
    resolved_page_size = page_size if page_size is not None else default_session_limit
    offset = (page - 1) * resolved_page_size
    session_result = await conversation_service.list_sessions(
        orchestration_id=orchestration_id,
        limit=resolved_page_size,
        offset=offset,
    )
    return success(
        data={
            "orchestration_id": orchestration_id,
            "page": page,
            "page_size": resolved_page_size,
            "total": session_result.total,
            "sessions": [
                _serialize_session_summary(item) for item in session_result.sessions
            ],
        }
    )


@router.get(
    "/api/orchestrations/{orchestration_id}/conversations/{session_id}",
    summary="查询编排应用单个会话详情",
)
async def get_orchestration_conversation(
    orchestration_id: str,
    session_id: str,
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int | None = Query(default=None, ge=1, description="每页数量"),
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> dict[str, object]:
    config = getattr(request.app.state, "config", {})
    conversation_config = config.get("conversation") if isinstance(config, dict) else {}
    if not isinstance(conversation_config, dict):
        conversation_config = {}
    default_turn_limit = int(conversation_config.get("turn_limit") or 50)
    resolved_page_size = page_size if page_size is not None else default_turn_limit
    offset = (page - 1) * resolved_page_size
    session_result = await conversation_service.get_session(
        orchestration_id=orchestration_id,
        session_id=session_id,
        limit=resolved_page_size,
        offset=offset,
    )
    return success(
        data={
            "orchestration_id": orchestration_id,
            "page": page,
            "page_size": resolved_page_size,
            "total": session_result.total,
            "session": _serialize_session(session_result.session)
            if session_result.session is not None
            else None,
        }
    )


@router.post(
    "/api/orchestrations/{orchestration_id}/conversations",
    summary="编排应用对话",
    response_model=None,
)
async def create_orchestration_conversation(
    orchestration_id: str,
    request: OrchestrationConversationRequest,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> object:
    try:
        if request.streaming:

            async def _event_stream() -> AsyncIterator[str]:
                async for item in service.stream_chat(
                    orchestration_id,
                    question=request.question,
                    response_mode=request.response_mode,
                    session_id=request.session_id,
                ):
                    yield encode_sse_event(str(item["event"]), item["data"])

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return success(
            data=await service.chat(
                orchestration_id,
                question=request.question,
                response_mode=request.response_mode,
                session_id=request.session_id,
            )
        )
    except OrchestrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="orchestration not found") from exc
    except LlmServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _serialize_session(session: ConversationRecord) -> dict[str, object]:
    return {
        "id": session.conversation_id,
        "orchestration_id": session.orchestration_id,
        "session_id": session.session_id,
        "machine_id": session.machine_id,
        "started_at": _serialize_datetime(session.started_at),
        "updated_at": _serialize_datetime(session.updated_at),
        "turn_count": len(session.turns),
        "turns": [_serialize_turn(turn) for turn in session.turns],
    }


def _serialize_session_summary(session: ConversationRecord) -> dict[str, object]:
    return {
        "id": session.conversation_id,
        "orchestration_id": session.orchestration_id,
        "session_id": session.session_id,
        "machine_id": session.machine_id,
        "started_at": _serialize_datetime(session.started_at),
        "updated_at": _serialize_datetime(session.updated_at),
        "turn_count": len(session.turns),
        "last_turn": _serialize_turn(session.turns[-1]) if session.turns else None,
    }


def _serialize_turn(turn: ConversationTurnRecord) -> dict[str, object]:
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "user_text": turn.user_text,
        "assistant_text": turn.assistant_text,
        "created_at": _serialize_datetime(turn.created_at),
    }


def _serialize_datetime(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp())


__all__ = [
    "get_conversation_service",
    "get_orchestration_service",
    "router",
]
