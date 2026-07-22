from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel
from websockets.asyncio.client import connect

SERVICE_NAME = "nexus-tts-service"
TTS_SAMPLE_RATE = 24000
TTS_CHANNELS = 1
DEFAULT_START_SESSION_RETRIES = 2

_PROTOCOL_VERSION = 0b0001
_DEFAULT_HEADER_SIZE = 0b0001
_FULL_CLIENT_REQUEST = 0b0001
_AUDIO_ONLY_RESPONSE = 0b1011
_FULL_SERVER_RESPONSE = 0b1001
_ERROR_INFORMATION = 0b1111
_MSG_TYPE_FLAG_WITH_EVENT = 0b0100
_JSON_SERIALIZATION = 0b0001

_EVENT_NONE = 0
_EVENT_START_SESSION = 100
_EVENT_CANCEL_SESSION = 101
_EVENT_FINISH_SESSION = 102
_EVENT_SESSION_STARTED = 150
_EVENT_SESSION_CANCELED = 151
_EVENT_SESSION_FINISHED = 152
_EVENT_SESSION_FAILED = 153
_EVENT_TASK_REQUEST = 200
_EVENT_TTS_RESPONSE = 352


class TtsStreamRequest(BaseModel):
    text: str
    voice: str | None = None


class HuoshanTtsBackend:
    def __init__(self) -> None:
        self.ws_url = _required_env(
            "HUOSHAN_TTS_WS_URL",
            "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
        )
        self.app_id = _required_env("HUOSHAN_TTS_APPID")
        self.access_token = _required_env("HUOSHAN_TTS_ACCESS_TOKEN")
        self.speaker = _required_env(
            "HUOSHAN_TTS_SPEAKER",
            "zh_female_qinqienvsheng_moon_bigtts",
        )
        self.resource_id = _required_env(
            "HUOSHAN_TTS_RESOURCE_ID",
            "volc.service_type.10029",
        )
        self.enable_ws_reuse = _parse_bool(os.getenv("HUOSHAN_TTS_ENABLE_WS_REUSE", "true"))
        self.start_session_retries = _parse_non_negative_int(
            os.getenv("HUOSHAN_TTS_START_SESSION_RETRIES"),
            DEFAULT_START_SESSION_RETRIES,
        )
        self.audio_params = {
            "format": "pcm",
            "sample_rate": TTS_SAMPLE_RATE,
            "speech_rate": 0,
            "loudness_rate": 0,
        }
        self.additions: dict[str, Any] = {}

        logger.info(
            "TTS backend configured | backend=huoshan ws_url={} resource_id={} speaker={} reuse={} start_retries={}",
            self.ws_url,
            self.resource_id,
            self.speaker,
            self.enable_ws_reuse,
            self.start_session_retries,
        )

    def metadata(self) -> dict[str, object]:
        return {
            "service": SERVICE_NAME,
            "backend": "huoshan",
            "model": "seed-tts",
            "speaker": self.speaker,
            "resource_id": self.resource_id,
            "sample_rate": TTS_SAMPLE_RATE,
            "channels": TTS_CHANNELS,
            "format": "pcm_s16le",
        }

    async def synthesize_stream(self, text: str, *, voice: str | None) -> AsyncIteratorBytes:
        speaker = _resolve_speaker(voice, default_speaker=self.speaker)
        client = HuoshanTtsClient(
            ws_url=self.ws_url,
            app_id=self.app_id,
            access_token=self.access_token,
            resource_id=self.resource_id,
            speaker=speaker,
            audio_params=self.audio_params,
            additions=self.additions,
            enable_ws_reuse=self.enable_ws_reuse,
        )
        try:
            await _start_session_with_retry(
                client,
                max_retries=self.start_session_retries,
            )
            await client.send_text(text)
            await client.finish_session()
            return _iter_client_audio(client)
        except Exception:
            await client.close()
            raise


def create_app() -> FastAPI:
    backend = HuoshanTtsBackend()
    app = FastAPI(title=SERVICE_NAME)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", **backend.metadata()})

    @app.get("/metadata")
    async def metadata() -> JSONResponse:
        return JSONResponse(backend.metadata())

    @app.post("/tts/stream")
    async def tts_stream(request: TtsStreamRequest) -> StreamingResponse:
        text = request.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        try:
            iterator = await backend.synthesize_stream(text, voice=request.voice)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return StreamingResponse(
            iterator,
            media_type="application/octet-stream",
            headers={
                "X-Audio-Format": "pcm_s16le",
                "X-Sample-Rate": str(TTS_SAMPLE_RATE),
                "X-Channels": str(TTS_CHANNELS),
            },
        )

    return app


AsyncIteratorBytes = Any


async def _iter_client_audio(client: "HuoshanTtsClient") -> AsyncIteratorBytes:
    try:
        while True:
            payload = await client.receive_audio()
            if payload is None:
                break
            yield payload
    finally:
        await client.close()


class HuoshanTtsClient:
    def __init__(
        self,
        *,
        ws_url: str,
        app_id: str,
        access_token: str,
        resource_id: str,
        speaker: str,
        audio_params: dict[str, Any],
        additions: dict[str, Any],
        enable_ws_reuse: bool,
    ) -> None:
        self._ws_url = ws_url
        self._app_id = app_id
        self._access_token = access_token
        self._resource_id = resource_id
        self._speaker = speaker
        self._audio_params = dict(audio_params)
        self._additions = dict(additions)
        self._enable_ws_reuse = enable_ws_reuse

        self._websocket: Any | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._session_error: Exception | None = None
        self._terminal_error: Exception | None = None
        self._session_started_event = asyncio.Event()
        self._closed = False

    async def start_session(self) -> None:
        if self._session_id is not None:
            await self.cancel_session()
        await self._ensure_connection()

        self._session_id = uuid.uuid4().hex
        self._audio_queue = asyncio.Queue()
        self._session_error = None
        self._terminal_error = None
        self._session_started_event = asyncio.Event()

        assert self._websocket is not None
        await _send_huoshan_event(
            self._websocket,
            event=_EVENT_START_SESSION,
            session_id=self._session_id,
            payload=_build_huoshan_payload(
                event=_EVENT_START_SESSION,
                speaker=self._speaker,
                audio_params=self._audio_params,
                additions=self._additions,
            ),
        )
        session_id = self._session_id
        logger.info("火山 TTS 会话启动请求已发送 | session_id={}", session_id)

        try:
            await asyncio.wait_for(self._session_started_event.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            error = self._session_error
            self._mark_session_complete(
                session_id=session_id,
                error=error or RuntimeError("火山 TTS 会话启动超时"),
                reason="start_timeout",
            )
            raise self._session_error or RuntimeError("火山 TTS 会话启动超时") from exc

        if self._session_error is not None:
            error = self._session_error
            self._session_error = None
            raise error

    async def send_text(self, text: str) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._session_error is not None:
            raise self._session_error
        session_id = self._require_session_id()
        assert self._websocket is not None
        await _send_huoshan_event(
            self._websocket,
            event=_EVENT_TASK_REQUEST,
            session_id=session_id,
            payload=_build_huoshan_payload(
                event=_EVENT_TASK_REQUEST,
                text=text,
                speaker=self._speaker,
                audio_params=self._audio_params,
                additions=self._additions,
            ),
        )

    async def finish_session(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._session_error is not None:
            raise self._session_error
        session_id = self._require_session_id()
        assert self._websocket is not None
        await _send_huoshan_event(
            self._websocket,
            event=_EVENT_FINISH_SESSION,
            session_id=session_id,
            payload=b"{}",
        )

    async def cancel_session(self) -> None:
        session_id = self._session_id
        if session_id is None:
            return
        websocket = self._websocket
        if websocket is not None:
            with contextlib.suppress(Exception):
                await _send_huoshan_event(
                    websocket,
                    event=_EVENT_CANCEL_SESSION,
                    session_id=session_id,
                    payload=b"{}",
                )
        self._mark_session_complete(
            session_id=session_id,
            error=None,
            reason="client_cancel",
        )

    async def receive_audio(self) -> bytes | None:
        payload = await self._audio_queue.get()
        if payload is not None:
            return payload
        if self._session_error is not None:
            error = self._session_error
            self._session_error = None
            raise error
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.cancel_session()

        task = self._monitor_task
        self._monitor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()

    def reset_for_retry(self) -> None:
        self._closed = False
        self._websocket = None
        self._monitor_task = None
        self._session_id = None
        self._audio_queue = asyncio.Queue()
        self._session_error = None
        self._terminal_error = None
        self._session_started_event = asyncio.Event()

    async def _ensure_connection(self) -> None:
        websocket = self._websocket
        if websocket is not None:
            if self._enable_ws_reuse and not bool(getattr(websocket, "closed", False)):
                return
            with contextlib.suppress(Exception):
                await websocket.close()
            self._websocket = None

        if self._websocket is None:
            headers = {
                "X-Api-App-Key": self._app_id,
                "X-Api-Access-Key": self._access_token,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Connect-Id": str(uuid.uuid4()),
            }
            self._websocket = await connect(
                self._ws_url,
                additional_headers=headers,
                max_size=1_000_000_000,
            )

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_responses(),
                name="nexus-huoshan-tts-monitor",
            )

    async def _monitor_responses(self) -> None:
        websocket = self._websocket
        if websocket is None:
            return

        try:
            while True:
                message = await websocket.recv()
                response = _parse_huoshan_response(message)
                session_id = response.session_id

                if response.error_code != 0:
                    detail = (
                        response.payload.decode("utf-8", errors="replace")
                        if response.payload
                        else ""
                    )
                    self._mark_session_complete(
                        session_id=self._session_id,
                        error=RuntimeError(
                            f"Huoshan TTS 返回错误 | code={response.error_code} detail={detail}"
                        ),
                        reason="error_packet",
                    )
                    continue

                if response.event == _EVENT_SESSION_STARTED:
                    self._session_started_event.set()
                    continue

                if (
                    response.event == _EVENT_TTS_RESPONSE
                    and response.message_type == _AUDIO_ONLY_RESPONSE
                    and response.payload is not None
                ):
                    if (
                        session_id
                        and self._session_id
                        and session_id != self._session_id
                    ):
                        continue
                    await self._audio_queue.put(response.payload)
                    continue

                if response.event == _EVENT_SESSION_CANCELED:
                    self._mark_session_complete(
                        session_id=session_id,
                        error=None,
                        reason="server_canceled",
                    )
                    continue

                if response.event == _EVENT_SESSION_FAILED:
                    detail = (
                        response.payload.decode("utf-8", errors="replace")
                        if response.payload
                        else response.response_meta or ""
                    )
                    self._mark_session_complete(
                        session_id=session_id,
                        error=RuntimeError(f"Huoshan TTS 会话失败 | detail={detail}"),
                        reason="server_failed",
                    )
                    continue

                if response.event == _EVENT_SESSION_FINISHED:
                    self._mark_session_complete(
                        session_id=session_id,
                        error=None,
                        reason="server_finished",
                    )
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_session_complete(
                session_id=self._session_id,
                error=RuntimeError(f"Huoshan TTS 连接异常 | {exc}"),
                reason="monitor_exception",
            )
            raise
        finally:
            if not self._enable_ws_reuse:
                websocket = self._websocket
                self._websocket = None
                if websocket is not None:
                    with contextlib.suppress(Exception):
                        await websocket.close()

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("火山 TTS 会话尚未启动")
        return self._session_id

    def _mark_session_complete(
        self,
        *,
        session_id: str | None,
        error: Exception | None,
        reason: str,
    ) -> None:
        active_session_id = self._session_id
        if active_session_id is None:
            return
        if session_id and session_id != active_session_id:
            return

        if error is not None:
            self._session_error = error
            self._terminal_error = error

        self._session_id = None
        self._session_started_event.set()
        logger.info(
            "火山 TTS 会话结束 | session_id={} reason={} has_error={}",
            active_session_id,
            reason,
            error is not None,
        )
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_queue.put_nowait(None)


@dataclass(slots=True)
class _HuoshanResponse:
    event: int = _EVENT_NONE
    message_type: int = 0
    session_id: str | None = None
    payload: bytes | None = None
    response_meta: str | None = None
    error_code: int = 0


def _required_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default or "").strip()
    if not value:
        raise RuntimeError(f"missing required env: {name}")
    return value


def _resolve_speaker(voice: str | None, *, default_speaker: str) -> str:
    normalized_voice = (voice or "").strip()
    if not normalized_voice or normalized_voice.lower() == "default":
        return default_speaker
    return normalized_voice


async def _start_session_with_retry(
    client: "HuoshanTtsClient",
    *,
    max_retries: int,
) -> None:
    attempts = max_retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await client.start_session()
            return
        except Exception as exc:
            last_error = exc
            await client.close()
            if attempt >= attempts:
                break
            delay_sec = min(0.2 * attempt, 1.0)
            logger.warning(
                "火山 TTS 会话启动失败，准备重试 | attempt={} max_attempts={} delay_sec={:.1f} error={}",
                attempt,
                attempts,
                delay_sec,
                exc,
            )
            await asyncio.sleep(delay_sec)
            client.reset_for_retry()
    assert last_error is not None
    raise last_error


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _build_huoshan_payload(
    *,
    event: int,
    speaker: str,
    audio_params: dict[str, Any],
    additions: dict[str, Any],
    text: str = "",
    uid: str = "1234",
) -> bytes:
    return json.dumps(
        {
            "user": {"uid": uid},
            "event": event,
            "namespace": "BidirectionalTTS",
            "req_params": {
                "text": text,
                "speaker": speaker,
                "audio_params": audio_params,
                "additions": json.dumps(additions),
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _build_huoshan_header() -> bytes:
    return bytes(
        [
            (_PROTOCOL_VERSION << 4) | _DEFAULT_HEADER_SIZE,
            (_FULL_CLIENT_REQUEST << 4) | _MSG_TYPE_FLAG_WITH_EVENT,
            (_JSON_SERIALIZATION << 4),
            0,
        ]
    )


def _build_huoshan_optional(*, event: int, session_id: str) -> bytes:
    optional = bytearray()
    optional.extend(event.to_bytes(4, "big", signed=True))
    session_id_bytes = session_id.encode("utf-8")
    optional.extend(len(session_id_bytes).to_bytes(4, "big", signed=True))
    optional.extend(session_id_bytes)
    return bytes(optional)


async def _send_huoshan_event(
    websocket: Any,
    *,
    event: int,
    session_id: str,
    payload: bytes,
) -> None:
    packet = bytearray(_build_huoshan_header())
    packet.extend(_build_huoshan_optional(event=event, session_id=session_id))
    packet.extend(len(payload).to_bytes(4, "big", signed=True))
    packet.extend(payload)
    await websocket.send(packet)


def _parse_huoshan_response(message: Any) -> _HuoshanResponse:
    if isinstance(message, str):
        raise RuntimeError(message)

    response = _HuoshanResponse()
    header1 = message[1]
    response.message_type = (header1 >> 4) & 0x0F
    flag = header1 & 0x0F
    offset = 4

    if response.message_type in {_FULL_SERVER_RESPONSE, _AUDIO_ONLY_RESPONSE}:
        if flag == _MSG_TYPE_FLAG_WITH_EVENT:
            response.event = int.from_bytes(
                message[offset : offset + 4], "big", signed=True
            )
            offset += 4
            if response.event == _EVENT_NONE:
                return response

            response.session_id, offset = _read_response_content(message, offset)
            if response.event in {
                _EVENT_SESSION_STARTED,
                _EVENT_SESSION_FAILED,
                _EVENT_SESSION_FINISHED,
            }:
                response.response_meta, offset = _read_response_content(message, offset)
                return response

            response.payload, _ = _read_response_payload(message, offset)
            return response

    if response.message_type == _ERROR_INFORMATION:
        response.error_code = int.from_bytes(
            message[offset : offset + 4], "big", signed=True
        )
        offset += 4
        response.payload, _ = _read_response_payload(message, offset)

    return response


def _read_response_content(message: bytes, offset: int) -> tuple[str, int]:
    content_size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
    offset += 4
    content = message[offset : offset + content_size].decode("utf-8")
    offset += content_size
    return content, offset


def _read_response_payload(message: bytes, offset: int) -> tuple[bytes, int]:
    payload_size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
    offset += 4
    payload = message[offset : offset + payload_size]
    offset += payload_size
    return payload, offset


app = create_app()
