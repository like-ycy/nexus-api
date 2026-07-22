"""客户端与云端之间共享的协议消息模型。"""

from __future__ import annotations

import threading
import time

from dataclasses import dataclass
from enum import Enum

from src.constants import (
    PROTOCOL_AUDIO_CHANNELS,
    PROTOCOL_AUDIO_CODEC,
    PROTOCOL_AUDIO_FRAME_MS,
    PROTOCOL_AUDIO_SAMPLE_RATE,
)

PROTOCOL_VERSION = "0.1.0"


class ClientMessageType(str, Enum):
    """客户端发往服务端的消息类型。"""

    AUDIO_START = "audio.start"
    AUDIO_STOP = "audio.stop"
    BINDING_ACK = "binding.ack"
    DEVICE_HELLO = "device.hello"
    INTERRUPT = "interrupt"
    LIVE_CAMERA_STATUS = "live_camera.status"
    TOOL_ACK = "tool.ack"
    TOOL_ERROR = "tool.error"
    VLA_DEBUG_CAMERA_FRAME = "vla.debug_camera.frame"
    TOOL_PROGRESS = "tool.progress"
    TOOL_RESULT = "tool.result"


class ServerMessageType(str, Enum):
    """服务端发往客户端的消息类型。"""

    ASR_RESULT = "asr.result"
    BINDING_CHANGED = "binding.changed"
    DEVICE_BOUND = "device.bound"
    DEVICE_UNBOUND = "device.unbound"
    INTERRUPT_ACK = "interrupt.ack"
    LLM_DELTA = "llm.delta"
    LIVE_CAMERA_START = "live_camera.start"
    LIVE_CAMERA_STOP = "live_camera.stop"
    SESSION_CONTROL = "session.control"
    SESSION_CREATED = "session.created"
    TOOL_CALL = "tool_call"
    TOOL_CANCEL = "tool.cancel"
    TOOL_INVOKE = "tool.invoke"
    VLA_DEBUG_CAMERA_SUBSCRIBE = "vla.debug_camera.subscribe"
    VLA_DEBUG_CAMERA_UNSUBSCRIBE = "vla.debug_camera.unsubscribe"
    TTS_START = "tts.start"
    TTS_STOP = "tts.stop"
    TTS_INTERRUPTED = "tts.interrupted"


@dataclass(frozen=True, slots=True)
class AudioStreamConfig:
    """音频流的协议级元信息。"""

    codec: str = PROTOCOL_AUDIO_CODEC
    sample_rate: int = PROTOCOL_AUDIO_SAMPLE_RATE
    channels: int = PROTOCOL_AUDIO_CHANNELS
    frame_ms: int = PROTOCOL_AUDIO_FRAME_MS


class MessageEnvelope:
    """为所有出站消息自动附加协议元信息。"""

    _seq_lock = threading.Lock()
    _seq_counter = 0

    @classmethod
    def _next_seq(cls) -> int:
        with cls._seq_lock:
            cls._seq_counter += 1
            return cls._seq_counter

    @classmethod
    def wrap(
        cls,
        payload: dict[str, object],
        *,
        turn_id: str | None = None,
    ) -> dict[str, object]:
        proto: dict[str, object] = {
            "version": PROTOCOL_VERSION,
            "seq": cls._next_seq(),
            "ts_ms": int(time.time() * 1000),
        }
        if turn_id is not None:
            proto["turn_id"] = turn_id
        payload["_proto"] = proto
        return payload

    @classmethod
    def reset(cls) -> None:
        with cls._seq_lock:
            cls._seq_counter = 0


__all__ = [
    "AudioStreamConfig",
    "ClientMessageType",
    "MessageEnvelope",
    "PROTOCOL_VERSION",
    "ServerMessageType",
]
