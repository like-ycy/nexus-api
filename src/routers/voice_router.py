"""声音列表与试听路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.routers.orchestration_router import get_orchestration_service
from src.services.orchestration import OrchestrationService
from src.utils.response import success

router = APIRouter(prefix="/api/voices", tags=["voices"])


class VoicePreviewRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="试听文案",
    )


@router.get("", summary="查询声音列表")
async def list_voices(
    service: OrchestrationService = Depends(get_orchestration_service),
) -> dict[str, object]:
    return success(data=await service.list_voices())


@router.post("/{voice_id}/preview", summary="试听指定声音")
async def preview_voice(
    voice_id: str,
    request: VoicePreviewRequest,
    service: OrchestrationService = Depends(get_orchestration_service),
) -> Response:
    try:
        audio_bytes = await service.preview_voice(
            voice_id=voice_id,
            text=request.text,
        )
        return Response(content=audio_bytes, media_type="audio/wav")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


__all__ = ["router"]
