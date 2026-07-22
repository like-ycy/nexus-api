"""云端 TTS 统一远端协议 provider。"""

from __future__ import annotations

import asyncio
import contextlib

from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any

import httpx
import numpy as np

from src.constants import (
    TTS_PROTOCOL_AUDIO_CHANNELS,
    TTS_PROTOCOL_AUDIO_FRAME_MS,
    TTS_PROTOCOL_AUDIO_SAMPLE_RATE,
)
from src.utils.logging import logger

TTS_SAMPLE_RATE = TTS_PROTOCOL_AUDIO_SAMPLE_RATE
TTS_CHANNELS = TTS_PROTOCOL_AUDIO_CHANNELS
TTS_FRAME_MS = TTS_PROTOCOL_AUDIO_FRAME_MS
DEFAULT_EMPTY_REPLY = "我这次没有听清楚，请再说一遍。"


class BaseTtsStreamClient(ABC):
    """统一 TTS 流式会话客户端。"""

    codec = "pcm"
    sample_rate = TTS_SAMPLE_RATE
    channels = TTS_CHANNELS
    frame_ms = TTS_FRAME_MS

    @abstractmethod
    async def start_session(self) -> None:
        """启动一轮 TTS 会话。"""

    @abstractmethod
    async def send_text(self, text: str) -> None:
        """向当前 TTS 会话发送文本。"""

    @abstractmethod
    async def finish_session(self) -> None:
        """标记当前 TTS 会话文本发送完毕。"""

    @abstractmethod
    async def cancel_session(self) -> None:
        """取消当前 TTS 会话。"""

    @abstractmethod
    async def receive_audio(self) -> bytes | None:
        """接收下一块 PCM 音频；返回 None 表示结束。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭客户端并释放连接资源。"""


class BaseTtsProvider(ABC):
    """TTS provider 统一接口。"""

    driver: str

    def supports_native_streaming(self) -> bool:
        return False

    def create_stream_client(self) -> BaseTtsStreamClient:
        raise NotImplementedError(f"{self.driver} 不支持原生流式 TTS")

    @abstractmethod
    def synthesize(self, text: str) -> np.ndarray:
        """将文本合成为单声道 float32 PCM。"""


class RemoteHttpTtsProvider(BaseTtsProvider):
    """通过统一 HTTP 流式协议调用外部 TTS 服务。"""

    driver = "remote_http_stream"

    def __init__(self, config: dict[str, Any]) -> None:
        self._stream_url = _require_text(config, "stream_url")
        self._voice = str(config.get("voice") or "default").strip() or "default"
        self._request_timeout_sec = float(config.get("request_timeout_sec") or 30.0)
        raw_headers = config.get("headers")
        raw_extra_body = config.get("extra_body")
        self._headers = _string_mapping(raw_headers)
        self._extra_body = (
            dict(raw_extra_body) if isinstance(raw_extra_body, dict) else {}
        )
        logger.info(
            "TTS 已加载 | driver={} stream_url={} voice={} timeout_sec={}",
            self.driver,
            self._stream_url,
            self._voice,
            self._request_timeout_sec,
        )

    def supports_native_streaming(self) -> bool:
        return True

    def create_stream_client(self) -> BaseTtsStreamClient:
        return RemoteHttpTtsStreamClient(
            stream_url=self._stream_url,
            voice=self._voice,
            headers=self._headers,
            extra_body=self._extra_body,
            request_timeout_sec=self._request_timeout_sec,
        )

    def synthesize(self, text: str) -> np.ndarray:
        raise NotImplementedError("远端 HTTP TTS 仅支持原生流式发送")


class RemoteHttpTtsStreamClient(BaseTtsStreamClient):
    """单个上游 HTTP 流式 TTS 会话。"""

    def __init__(
        self,
        *,
        stream_url: str,
        voice: str,
        headers: dict[str, str],
        extra_body: dict[str, Any],
        request_timeout_sec: float,
    ) -> None:
        self._stream_url = stream_url
        self._voice = voice
        self._headers = dict(headers)
        self._extra_body = dict(extra_body)
        self._request_timeout_sec = request_timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._session_active = False
        self._session_error: Exception | None = None
        self._cancel_requested = False
        self._stream_closed = False
        self._active_response: httpx.Response | None = None
        self._wav_decoder: _WavStreamDecoder | None = None

    async def start_session(self) -> None:
        if self._session_active:
            await self.cancel_session()
        await self._ensure_client()
        self._audio_queue = asyncio.Queue()
        self._session_active = True
        self._session_error = None
        self._cancel_requested = False
        self._stream_closed = False
        logger.debug("远端 TTS 会话已启动 | voice={}", self._voice)

    async def send_text(self, text: str) -> None:
        if not self._session_active:
            raise RuntimeError("远端 TTS 会话尚未启动")
        if self._cancel_requested:
            return

        normalized_text = text.strip() or DEFAULT_EMPTY_REPLY
        payload = {
            "text": normalized_text,
            "voice": self._voice,
            **self._extra_body,
        }
        started_at = perf_counter()
        logger.debug(
            "远端 TTS 请求开始 | text_len={} voice={} timeout_sec={}",
            len(normalized_text),
            self._voice,
            self._request_timeout_sec,
        )

        response: httpx.Response | None = None
        try:
            client = await self._ensure_client()
            async with client.stream(
                "POST",
                self._stream_url,
                headers=self._headers or None,
                json=payload,
            ) as response:
                self._active_response = response
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"远端 TTS 请求失败 | status={response.status_code} detail={detail[:200]}"
                    )

                content_type = str(response.headers.get("content-type") or "").lower()
                is_wav_stream = (
                    "audio/wav" in content_type or "audio/x-wav" in content_type
                )
                self._wav_decoder = _WavStreamDecoder() if is_wav_stream else None
                logger.debug(
                    "远端 TTS 响应已建立 | content_type={} decode_mode={}",
                    content_type or None,
                    "wav_to_pcm" if is_wav_stream else "raw_pcm",
                )

                first_chunk_at: float | None = None
                async for chunk in response.aiter_bytes():
                    if self._cancel_requested:
                        break
                    if not chunk:
                        continue

                    if self._wav_decoder is not None:
                        chunk = self._wav_decoder.feed(chunk)
                        if not chunk:
                            continue

                    if first_chunk_at is None:
                        first_chunk_at = perf_counter()
                        logger.debug(
                            "远端 TTS 首个 PCM chunk 到达 | elapsed_ms={:.1f} chunk_bytes={}",
                            (first_chunk_at - started_at) * 1000,
                            len(chunk),
                        )
                    await self._audio_queue.put(chunk)

            logger.debug(
                "远端 TTS 请求完成 | text_len={} elapsed_ms={:.1f} canceled={}",
                len(normalized_text),
                (perf_counter() - started_at) * 1000,
                self._cancel_requested,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._session_error = exc
            await self._close_stream(error=exc, reason="request_failed")
            raise
        finally:
            if self._active_response is response:
                self._active_response = None
            self._wav_decoder = None

    async def finish_session(self) -> None:
        await self._close_stream(error=None, reason="finished")

    async def cancel_session(self) -> None:
        self._cancel_requested = True
        response = self._active_response
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()
        await self._close_stream(error=None, reason="canceled")

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
        await self.cancel_session()
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self._request_timeout_sec, connect=5.0)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def _close_stream(
        self,
        *,
        error: Exception | None,
        reason: str,
    ) -> None:
        if not self._session_active and self._stream_closed:
            return

        if error is not None:
            self._session_error = error

        self._session_active = False
        if self._stream_closed:
            return
        self._stream_closed = True
        logger.debug(
            "远端 TTS 会话结束 | voice={} reason={} has_error={}",
            self._voice,
            reason,
            error is not None,
        )
        with contextlib.suppress(asyncio.QueueFull):
            self._audio_queue.put_nowait(None)


class _WavStreamDecoder:
    """将 HTTP 分块返回的 WAV 数据流转换为裸 PCM 数据流。"""

    def __init__(self) -> None:
        self._header_buffer = bytearray()
        self._header_processed = False

    def feed(self, chunk: bytes) -> bytes:
        if self._header_processed:
            return chunk

        self._header_buffer.extend(chunk)
        data_offset = self._find_data_offset(self._header_buffer)
        if data_offset is None:
            return b""

        self._header_processed = True
        payload = bytes(self._header_buffer[data_offset:])
        self._header_buffer.clear()
        return payload

    @staticmethod
    def _find_data_offset(buffer: bytearray) -> int | None:
        if len(buffer) < 12:
            return None
        if bytes(buffer[:4]) != b"RIFF" or bytes(buffer[8:12]) != b"WAVE":
            return 0

        offset = 12
        while offset + 8 <= len(buffer):
            chunk_id = bytes(buffer[offset : offset + 4])
            chunk_size = int.from_bytes(buffer[offset + 4 : offset + 8], "little")
            next_offset = offset + 8 + chunk_size
            if chunk_size % 2 == 1:
                next_offset += 1

            if chunk_id == b"data":
                if next_offset > len(buffer) and offset + 8 > len(buffer):
                    return None
                return offset + 8

            if next_offset > len(buffer):
                return None
            offset = next_offset

        return None


def build_tts_provider(
    *,
    driver: str,
    config: dict[str, Any],
) -> BaseTtsProvider:
    normalized_driver = driver.strip()
    if normalized_driver == RemoteHttpTtsProvider.driver:
        return RemoteHttpTtsProvider(config)
    raise ValueError(f"当前暂不支持的 TTS driver: {normalized_driver}")


def _require_text(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ValueError(f"TTS 配置缺少字段: {key}")
    return value


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _pcm16_bytes_to_float32(payload: bytes) -> np.ndarray:
    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    return np.clip(samples / 32768.0, -1.0, 1.0)


TextToSpeechEngine = RemoteHttpTtsProvider

__all__ = [
    "BaseTtsProvider",
    "BaseTtsStreamClient",
    "RemoteHttpTtsProvider",
    "RemoteHttpTtsStreamClient",
    "TextToSpeechEngine",
    "_WavStreamDecoder",
    "_pcm16_bytes_to_float32",
    "build_tts_provider",
]
