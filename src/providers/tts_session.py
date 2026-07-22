"""TTS 会话执行与 Opus 回传。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import numpy as np

from src.constants import (
    TTS_PROTOCOL_AUDIO_CHANNELS,
    TTS_PROTOCOL_AUDIO_FRAME_MS,
    TTS_PROTOCOL_AUDIO_SAMPLE_RATE,
)
from src.protocol.edge_cloud import MessageEnvelope, ServerMessageType
from src.utils.logging import logger
from src.utils.opus_loader import setup_opus

setup_opus()

TTS_SAMPLE_RATE = TTS_PROTOCOL_AUDIO_SAMPLE_RATE
TTS_CHANNELS = TTS_PROTOCOL_AUDIO_CHANNELS
TTS_FRAME_MS = TTS_PROTOCOL_AUDIO_FRAME_MS
TTS_FRAME_SIZE = int(TTS_SAMPLE_RATE * (TTS_FRAME_MS / 1000))
TTS_OPUS_COMPLEXITY = 10
TTS_FIRST_SEGMENT_PUNCTUATION = "，,、。！？!?；;：:\n"
TTS_SEGMENT_PUNCTUATION = "。！？!?；;：:\n"
TTS_FIRST_SEGMENT_SOFT_LIMIT = 10
TTS_SEGMENT_SOFT_LIMIT = 30
TTS_SOFT_BREAK_CHARS = "，,、；;：: ）)】]}> \n"


@dataclass(frozen=True, slots=True)
class TtsStreamMetrics:
    first_packet_ms: float | None
    total_elapsed_ms: float
    frame_count: int
    text_len: int
    segment_count: int
    mode: str


class TtsPlaybackSession:
    """负责单个连接内的一次次 TTS 文本执行与音频回传。"""

    def __init__(
        self,
        websocket: Any,
        tts_provider: Any,
        *,
        inference_lock: threading.Lock | None = None,
    ) -> None:
        self.websocket = websocket
        self._tts_provider = tts_provider
        self._inference_lock = inference_lock
        self.encoder: Any | None = None
        self._native_stream_client = (
            tts_provider.create_stream_client()
            if tts_provider is not None
            and getattr(tts_provider, "supports_native_streaming", None) is not None
            and tts_provider.supports_native_streaming()
            else None
        )

    async def stream_text(self, text: str) -> TtsStreamMetrics:
        started_at = perf_counter()
        normalized_text = sanitize_tts_text(text)
        segments = split_tts_segments(normalized_text)
        if not segments:
            return TtsStreamMetrics(
                first_packet_ms=None,
                total_elapsed_ms=(perf_counter() - started_at) * 1000,
                frame_count=0,
                text_len=0,
                segment_count=0,
                mode="empty",
            )

        if self._native_stream_client is not None:
            (
                frame_count,
                native_first_packet_ms,
            ) = await self._stream_segments_with_native_tts(
                segments,
                full_text=normalized_text,
                stream_started_at=started_at,
            )
            return TtsStreamMetrics(
                first_packet_ms=native_first_packet_ms,
                total_elapsed_ms=(perf_counter() - started_at) * 1000,
                frame_count=frame_count,
                text_len=len(normalized_text),
                segment_count=len(segments),
                mode="native",
            )

        await self._send_tts_start(normalized_text)

        synth_task: asyncio.Task[np.ndarray] | None = asyncio.create_task(
            asyncio.to_thread(self._synthesize_reply, segments[0]),
            name="nexus-session-tts-synth-0",
        )
        total_frames = 0
        first_packet_ms: float | None = None

        for index, segment in enumerate(segments):
            assert synth_task is not None
            samples = await synth_task

            next_task: asyncio.Task[np.ndarray] | None = None
            if index + 1 < len(segments):
                next_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._synthesize_reply,
                        segments[index + 1],
                    ),
                    name=f"nexus-session-tts-synth-{index + 1}",
                )

            frames, segment_first_packet_ms = await self._send_tts_packets(
                samples,
                segment_index=index,
                segment_text=segment,
                stream_started_at=started_at,
            )
            total_frames += frames
            if first_packet_ms is None:
                first_packet_ms = segment_first_packet_ms
            synth_task = next_task

        await self._send_tts_stop(
            normalized_text,
            frame_count=total_frames,
        )
        return TtsStreamMetrics(
            first_packet_ms=first_packet_ms,
            total_elapsed_ms=(perf_counter() - started_at) * 1000,
            frame_count=total_frames,
            text_len=len(normalized_text),
            segment_count=len(segments),
            mode="buffered",
        )

    async def stream_text_queue(
        self,
        queue: asyncio.Queue[str | None],
    ) -> TtsStreamMetrics:
        started_at = perf_counter()
        if self._native_stream_client is not None:
            return await self._stream_text_queue_with_native_tts(
                queue,
                stream_started_at=started_at,
            )

        full_text_parts: list[str] = []
        pending_text = ""
        total_frames = 0
        is_first_segment = True
        started = False
        segment_index = 0
        first_packet_ms: float | None = None

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if not chunk:
                continue

            normalized_chunk = sanitize_tts_text(chunk)
            if not normalized_chunk:
                continue

            full_text_parts.append(normalized_chunk)
            pending_text += normalized_chunk
            segments, pending_text, is_first_segment = _drain_ready_segments(
                pending_text,
                is_first_segment=is_first_segment,
            )
            for segment in segments:
                if not started:
                    await self._send_tts_start(segment)
                    started = True
                frames, segment_first_packet_ms = await self._stream_single_segment(
                    segment,
                    segment_index=segment_index,
                    stream_started_at=started_at,
                )
                total_frames += frames
                if first_packet_ms is None:
                    first_packet_ms = segment_first_packet_ms
                segment_index += 1

        tail = pending_text.strip()
        if tail:
            if not started:
                await self._send_tts_start(tail)
                started = True
            frames, segment_first_packet_ms = await self._stream_single_segment(
                tail,
                segment_index=segment_index,
                stream_started_at=started_at,
            )
            total_frames += frames
            if first_packet_ms is None:
                first_packet_ms = segment_first_packet_ms

        full_text = "".join(full_text_parts).strip()
        if not started:
            full_text = full_text or "我这次没有听清楚，请再说一遍。"
            await self._send_tts_start(full_text)
        await self._send_tts_stop(
            full_text,
            frame_count=total_frames,
        )
        return TtsStreamMetrics(
            first_packet_ms=first_packet_ms,
            total_elapsed_ms=(perf_counter() - started_at) * 1000,
            frame_count=total_frames,
            text_len=len(full_text),
            segment_count=max(
                segment_index + (1 if tail else 0), 1 if full_text else 0
            ),
            mode="stream_queue",
        )

    async def cancel_active_stream(self) -> None:
        client = self._native_stream_client
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.cancel_session()

    async def close(self) -> None:
        client = self._native_stream_client
        self._native_stream_client = None
        if client is None:
            return
        await client.close()

    async def notify_interrupted(self, session_id: str | None) -> None:
        await self.websocket.send(
            json.dumps(
                MessageEnvelope.wrap(
                    {
                        "type": ServerMessageType.TTS_INTERRUPTED.value,
                        "session_id": session_id,
                    }
                ),
                ensure_ascii=False,
            )
        )

    async def _send_tts_start(self, text: str) -> None:
        self._ensure_encoder()
        await self.websocket.send(
            json.dumps(
                MessageEnvelope.wrap(
                    {
                        "type": ServerMessageType.TTS_START.value,
                        "codec": "opus",
                        "sample_rate": TTS_SAMPLE_RATE,
                        "channels": TTS_CHANNELS,
                        "frame_ms": TTS_FRAME_MS,
                        "text": text,
                    }
                ),
                ensure_ascii=False,
            )
        )

    async def _send_tts_packets(
        self,
        samples: np.ndarray,
        *,
        segment_index: int | None = None,
        segment_text: str | None = None,
        synth_started_at: float | None = None,
        stream_started_at: float | None = None,
    ) -> tuple[int, float | None]:
        self._ensure_encoder()
        packets = self._encode_tts_packets(samples)
        frame_count = 0
        first_packet_logged = False
        first_packet_elapsed_ms: float | None = None

        warmup_packets = min(8, len(packets))
        for packet in packets[:warmup_packets]:
            await self.websocket.send(packet)
            frame_count += 1
            if not first_packet_logged:
                first_packet_elapsed_ms = self._log_first_tts_packet(
                    segment_index=segment_index,
                    segment_text=segment_text,
                    synth_started_at=synth_started_at,
                    stream_started_at=stream_started_at,
                )
                first_packet_logged = True

        batch_size = 4
        pacing_sleep_sec = (TTS_FRAME_MS / 1000) * 2
        for start in range(warmup_packets, len(packets), batch_size):
            for packet in packets[start : start + batch_size]:
                await self.websocket.send(packet)
                frame_count += 1
                if not first_packet_logged:
                    first_packet_elapsed_ms = self._log_first_tts_packet(
                        segment_index=segment_index,
                        segment_text=segment_text,
                        synth_started_at=synth_started_at,
                        stream_started_at=stream_started_at,
                    )
                    first_packet_logged = True
            await asyncio.sleep(pacing_sleep_sec)

        return frame_count, first_packet_elapsed_ms

    async def _send_tts_stop(self, text: str, *, frame_count: int) -> None:
        await self.websocket.send(
            json.dumps(
                MessageEnvelope.wrap(
                    {
                        "type": ServerMessageType.TTS_STOP.value,
                        "text": text,
                        "frames": frame_count,
                    }
                ),
                ensure_ascii=False,
            )
        )

    async def _stream_single_segment(
        self,
        segment: str,
        *,
        segment_index: int | None = None,
        stream_started_at: float,
    ) -> tuple[int, float | None]:
        normalized_segment = sanitize_tts_text(segment)
        synth_started_at = perf_counter()
        samples = await asyncio.to_thread(self._synthesize_reply, normalized_segment)
        return await self._send_tts_packets(
            samples,
            segment_index=segment_index,
            segment_text=normalized_segment,
            synth_started_at=synth_started_at,
            stream_started_at=stream_started_at,
        )

    async def _stream_segments_with_native_tts(
        self,
        segments: list[str],
        *,
        full_text: str,
        stream_started_at: float,
    ) -> tuple[int, float | None]:
        client = self._require_native_stream_client()
        total_frames = 0
        native_first_packet_ms: float | None = None
        receiver_task: asyncio.Task[tuple[int, float | None]] | None = None

        try:
            await self._send_tts_start(segments[0])
            await client.start_session()
            receiver_task = asyncio.create_task(
                self._forward_native_audio_stream(stream_started_at=stream_started_at),
                name="nexus-tts-native-recv",
            )

            for index, segment in enumerate(segments):
                await client.send_text(segment)

            await client.finish_session()
            total_frames, native_first_packet_ms = await receiver_task
        except asyncio.CancelledError:
            await self.cancel_active_stream()
            if receiver_task is not None:
                with contextlib.suppress(Exception):
                    await receiver_task
            raise
        except Exception:
            await self.cancel_active_stream()
            if receiver_task is not None:
                with contextlib.suppress(Exception):
                    await receiver_task
            raise

        await self._send_tts_stop(
            full_text,
            frame_count=total_frames,
        )
        return total_frames, native_first_packet_ms

    async def _stream_text_queue_with_native_tts(
        self,
        queue: asyncio.Queue[str | None],
        *,
        stream_started_at: float,
    ) -> TtsStreamMetrics:
        client = self._require_native_stream_client()
        full_text_parts: list[str] = []
        pending_text = ""
        total_frames = 0
        is_first_segment = True
        started = False
        segment_index = 0
        receiver_task: asyncio.Task[tuple[int, float | None]] | None = None
        first_packet_ms: float | None = None

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if not chunk:
                    continue

                normalized_chunk = sanitize_tts_text(chunk)
                if not normalized_chunk:
                    continue

                full_text_parts.append(normalized_chunk)
                pending_text += normalized_chunk
                segments, pending_text, is_first_segment = _drain_ready_segments(
                    pending_text,
                    is_first_segment=is_first_segment,
                )
                for segment in segments:
                    if not started:
                        await self._send_tts_start(segment)
                        await client.start_session()
                        receiver_task = asyncio.create_task(
                            self._forward_native_audio_stream(
                                stream_started_at=stream_started_at
                            ),
                            name="nexus-tts-native-recv",
                        )
                        started = True
                    await client.send_text(segment)
                    segment_index += 1

            tail = pending_text.strip()
            if tail:
                if not started:
                    await self._send_tts_start(tail)
                    await client.start_session()
                    receiver_task = asyncio.create_task(
                        self._forward_native_audio_stream(
                            stream_started_at=stream_started_at
                        ),
                        name="nexus-tts-native-recv",
                    )
                    started = True
                await client.send_text(tail)
                segment_index += 1

            full_text = "".join(full_text_parts).strip()
            if not started:
                full_text = full_text or "我这次没有听清楚，请再说一遍。"
                await self._send_tts_start(full_text)
                await client.start_session()
                receiver_task = asyncio.create_task(
                    self._forward_native_audio_stream(
                        stream_started_at=stream_started_at
                    ),
                    name="nexus-tts-native-recv",
                )
                await client.send_text(full_text)
                started = True
                segment_index = 1

            await client.finish_session()
            if receiver_task is not None:
                total_frames, first_packet_ms = await receiver_task
        except asyncio.CancelledError:
            await self.cancel_active_stream()
            if receiver_task is not None:
                with contextlib.suppress(Exception):
                    await receiver_task
            raise
        except Exception:
            await self.cancel_active_stream()
            if receiver_task is not None:
                with contextlib.suppress(Exception):
                    await receiver_task
            raise

        await self._send_tts_stop(
            full_text,
            frame_count=total_frames,
        )
        return TtsStreamMetrics(
            first_packet_ms=first_packet_ms,
            total_elapsed_ms=(perf_counter() - stream_started_at) * 1000,
            frame_count=total_frames,
            text_len=len(full_text),
            segment_count=segment_index,
            mode="native_stream_queue",
        )

    async def _forward_native_audio_stream(
        self,
        *,
        stream_started_at: float,
    ) -> tuple[int, float | None]:
        client = self._require_native_stream_client()
        self._ensure_encoder()
        encoder = self.encoder
        if encoder is None:
            raise RuntimeError("TTS Opus 编码器初始化失败")

        frame_bytes = TTS_FRAME_SIZE * TTS_CHANNELS * 2
        buffer = bytearray()
        frame_count = 0
        first_packet_logged = False
        first_packet_elapsed_ms: float | None = None

        while True:
            payload = await client.receive_audio()
            if payload is None:
                break

            buffer.extend(payload)
            while len(buffer) >= frame_bytes:
                frame = bytes(buffer[:frame_bytes])
                del buffer[:frame_bytes]
                await self.websocket.send(encoder.encode(frame, TTS_FRAME_SIZE))
                frame_count += 1
                if not first_packet_logged:
                    first_packet_elapsed_ms = (
                        perf_counter() - stream_started_at
                    ) * 1000
                    first_packet_logged = True

        if buffer:
            padded = bytearray(frame_bytes)
            padded[: len(buffer)] = buffer
            await self.websocket.send(encoder.encode(bytes(padded), TTS_FRAME_SIZE))
            frame_count += 1
            if not first_packet_logged:
                first_packet_elapsed_ms = (perf_counter() - stream_started_at) * 1000

        return frame_count, first_packet_elapsed_ms

    def _log_first_tts_packet(
        self,
        *,
        segment_index: int | None,
        segment_text: str | None,
        synth_started_at: float | None,
        stream_started_at: float | None,
    ) -> float | None:
        _ = segment_index, segment_text, synth_started_at
        if stream_started_at is None:
            return None
        return (perf_counter() - stream_started_at) * 1000

    def _encode_tts_packets(self, samples: np.ndarray) -> list[bytes]:
        if self.encoder is None:
            raise RuntimeError("Opus 编码器未初始化")

        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        packets: list[bytes] = []

        for offset in range(0, mono.size, TTS_FRAME_SIZE):
            frame = mono[offset : offset + TTS_FRAME_SIZE]
            if frame.size < TTS_FRAME_SIZE:
                padded = np.zeros(TTS_FRAME_SIZE, dtype=np.float32)
                padded[: frame.size] = frame
                frame = padded

            pcm = (
                np.rint(np.clip(frame, -1.0, 32767.0 / 32768.0) * 32767.0)
                .astype(np.int16)
                .tobytes()
            )
            packets.append(self.encoder.encode(pcm, TTS_FRAME_SIZE))

        return packets

    def _ensure_encoder(self) -> None:
        if self.encoder is not None:
            return

        opuslib = __import__("opuslib")
        encoder_cls = getattr(opuslib, "Encoder")
        application = getattr(
            opuslib, "APPLICATION_AUDIO", getattr(opuslib, "APPLICATION_VOIP")
        )
        self.encoder = encoder_cls(TTS_SAMPLE_RATE, TTS_CHANNELS, application)
        self._configure_encoder()

    def _configure_encoder(self) -> None:
        if self.encoder is None:
            return

        settings: tuple[tuple[str, object], ...] = (
            ("complexity", TTS_OPUS_COMPLEXITY),
            ("vbr", True),
            ("vbr_constraint", True),
        )

        applied: dict[str, object] = {}
        for name, value in settings:
            try:
                setattr(self.encoder, name, value)
                applied[name] = value
            except Exception as exc:
                logger.warning(
                    "设置 TTS Opus 编码参数失败 | {}={!r} err={}", name, value, exc
                )

        if applied:
            logger.debug(
                "TTS Opus 编码参数已启用 | sample_rate={}Hz channels={}ch frame={}ms params={}",
                TTS_SAMPLE_RATE,
                TTS_CHANNELS,
                TTS_FRAME_MS,
                applied,
            )

    def _synthesize_reply(self, reply_text: str) -> np.ndarray:
        if self._tts_provider is None:
            raise ValueError("TTS 合成失败: 缺少 provider")
        if self._inference_lock is None:
            return self._tts_provider.synthesize(reply_text)
        with self._inference_lock:
            return self._tts_provider.synthesize(reply_text)

    def _require_native_stream_client(self) -> Any:
        if self._native_stream_client is None:
            raise RuntimeError("当前 TTS provider 不支持原生流式")
        return self._native_stream_client


def split_tts_segments(text: str) -> list[str]:
    """按句切分回复文本，优先让首句更早开始播放。"""
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if not normalized:
        return ["我这次没有听清楚，请再说一遍。"]

    segments: list[str] = []
    buffer: list[str] = []
    is_first_segment = True

    for char in normalized:
        buffer.append(char)
        punctuation = (
            TTS_FIRST_SEGMENT_PUNCTUATION
            if is_first_segment
            else TTS_SEGMENT_PUNCTUATION
        )
        if char not in punctuation:
            continue

        segment = "".join(buffer).strip()
        buffer.clear()
        if not segment:
            continue
        segments.append(segment)
        is_first_segment = False

    tail = "".join(buffer).strip()
    if tail:
        segments.append(tail)

    return segments or ["我这次没有听清楚，请再说一遍。"]


def sanitize_tts_text(text: str) -> str:
    normalized = re.sub(r"\r\n?", "\n", text)
    normalized = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", normalized)
    normalized = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", normalized)
    normalized = re.sub(r"`([^`]*)`", r"\1", normalized)
    normalized = normalized.replace("**", "")
    normalized = normalized.replace("__", "")
    normalized = normalized.replace("~~", "")
    normalized = re.sub(r"^[>\-#*\s]+", "", normalized, flags=re.MULTILINE)
    normalized = re.sub(r"[*_~`]+", "", normalized)
    normalized = re.sub(r"[✨⭐🌟💫🔥🎉✅☑️✔️•◆■□●]+", "", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _drain_ready_segments(
    text: str,
    *,
    is_first_segment: bool,
) -> tuple[list[str], str, bool]:
    segments: list[str] = []
    current_first = is_first_segment
    remaining = text

    while remaining:
        punctuation = (
            TTS_FIRST_SEGMENT_PUNCTUATION if current_first else TTS_SEGMENT_PUNCTUATION
        )
        split_index = _find_first_punctuation_index(remaining, punctuation)
        if split_index >= 0:
            segment = remaining[: split_index + 1].strip()
            remaining = remaining[split_index + 1 :]
            if segment:
                segments.append(segment)
                current_first = False
            continue

        split_index = _find_soft_split_index(
            remaining,
            is_first_segment=current_first,
        )
        if split_index <= 0:
            break

        segment = remaining[:split_index].strip()
        remaining = remaining[split_index:]
        if not segment:
            continue
        logger.debug(
            "流式 TTS 提前切分 | is_first_segment={} segment_len={} text={}",
            current_first,
            len(segment),
            segment,
        )
        segments.append(segment)
        current_first = False

    return segments, remaining, current_first


def _find_first_punctuation_index(text: str, punctuation: str) -> int:
    for index, char in enumerate(text):
        if char in punctuation:
            return index
    return -1


def _find_soft_split_index(text: str, *, is_first_segment: bool) -> int:
    normalized = text.strip()
    if not normalized:
        return -1

    soft_limit = (
        TTS_FIRST_SEGMENT_SOFT_LIMIT if is_first_segment else TTS_SEGMENT_SOFT_LIMIT
    )
    if len(normalized) < soft_limit:
        return -1

    candidate = text[:soft_limit]
    for index in range(len(candidate) - 1, -1, -1):
        if candidate[index] in TTS_SOFT_BREAK_CHARS:
            return index + 1
    return soft_limit


__all__ = [
    "TtsStreamMetrics",
    "TtsPlaybackSession",
    "sanitize_tts_text",
    "split_tts_segments",
]
