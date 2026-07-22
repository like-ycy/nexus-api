"""连接级音频处理线程。"""

from __future__ import annotations

import queue
import threading

from concurrent.futures import Future
from pathlib import Path
from typing import Any

import numpy as np

from src.constants import (
    PROTOCOL_AUDIO_CHANNELS,
    PROTOCOL_AUDIO_FRAME_MS,
    PROTOCOL_AUDIO_SAMPLE_RATE,
)
from src.providers.vad import SileroVadEngine
from src.utils.logging import logger
from src.utils.opus_loader import setup_opus

setup_opus()

SERVER_SAMPLE_RATE = PROTOCOL_AUDIO_SAMPLE_RATE
SERVER_CHANNELS = PROTOCOL_AUDIO_CHANNELS
SERVER_FRAME_MS = PROTOCOL_AUDIO_FRAME_MS


class AudioStreamWorker:
    """将 Opus 解码与 VAD 累积放到独立线程中处理。"""

    def __init__(self, vad_model_path: Path) -> None:
        self._vad_model_path = vad_model_path
        self._command_queue: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._desired_stream_id = 0
        self._stream_id_lock = threading.Lock()

        self._active_stream_id = 0
        self._stream_config: dict[str, Any] = {}
        self._decoder: Any | None = None
        self._vad = SileroVadEngine(vad_model_path)
        self._speech_segments: list[np.ndarray] = []
        self._packets_received = 0
        self._bytes_received = 0
        self._accepting_audio = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="nexus-audio-worker",
            daemon=True,
        )
        self._thread.start()

    def start_stream(self, stream_id: int, stream_config: dict[str, Any]) -> None:
        self._set_desired_stream_id(stream_id)
        self._command_queue.put(("start_stream", (stream_id, dict(stream_config))))

    def enqueue_packet(self, stream_id: int, packet: bytes) -> None:
        self._command_queue.put(("audio_packet", (stream_id, packet)))

    def finish_stream(self, stream_id: int, *, flush_vad: bool) -> Future[np.ndarray]:
        future: Future[np.ndarray] = Future()
        self._command_queue.put(("finish_stream", (stream_id, flush_vad, future)))
        return future

    def reset_stream(self, stream_id: int) -> None:
        self._set_desired_stream_id(stream_id)
        self._command_queue.put(("reset_stream", (stream_id,)))

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return

        future: Future[None] = Future()
        self._command_queue.put(("close", (future,)))
        future.result(timeout=5)
        thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while True:
            command, args = self._command_queue.get()
            try:
                if command == "start_stream":
                    stream_id, stream_config = args
                    self._handle_start_stream(stream_id, stream_config)
                    continue

                if command == "audio_packet":
                    stream_id, packet = args
                    self._handle_audio_packet(stream_id, packet)
                    continue

                if command == "finish_stream":
                    stream_id, flush_vad, future = args
                    self._handle_finish_stream(stream_id, flush_vad, future)
                    continue

                if command == "reset_stream":
                    (stream_id,) = args
                    self._handle_reset_stream(stream_id)
                    continue

                if command == "close":
                    (future,) = args
                    self._reset_state(active_stream_id=self._active_stream_id)
                    future.set_result(None)
                    return
            finally:
                self._command_queue.task_done()

    def _handle_start_stream(
        self,
        stream_id: int,
        stream_config: dict[str, Any],
    ) -> None:
        if stream_id != self._desired_stream_id:
            return

        self._active_stream_id = stream_id
        self._stream_config = dict(stream_config)
        self._decoder = self._create_opus_decoder(stream_config)
        self._vad.reset()
        self._speech_segments.clear()
        self._packets_received = 0
        self._bytes_received = 0
        self._accepting_audio = True

    def _handle_audio_packet(self, stream_id: int, packet: bytes) -> None:
        if stream_id != self._desired_stream_id:
            return
        if stream_id != self._active_stream_id:
            return
        if self._decoder is None or not self._accepting_audio:
            return

        self._packets_received += 1
        self._bytes_received += len(packet)

        try:
            pcm_bytes = self._decoder.decode(packet, self._input_frame_size())
        except Exception as exc:
            logger.warning("Opus 音频解码失败: {}", exc)
            return

        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if pcm.size == 0:
            return

        segments = self._vad.accept_waveform(pcm)
        if segments:
            self._speech_segments.extend(segments)

        if self._packets_received == 1 or self._packets_received % 25 == 0:
            logger.debug(
                "收到音频帧 | packets={} bytes={} last_packet={}B",
                self._packets_received,
                self._bytes_received,
                len(packet),
            )

    def _handle_finish_stream(
        self,
        stream_id: int,
        flush_vad: bool,
        future: Future[np.ndarray],
    ) -> None:
        try:
            if (
                stream_id != self._desired_stream_id
                or stream_id != self._active_stream_id
            ):
                future.set_result(np.empty(0, dtype=np.float32))
                return

            if flush_vad:
                self._speech_segments.extend(self._vad.flush())

            if self._speech_segments:
                utterance = np.concatenate(self._speech_segments).astype(
                    np.float32,
                    copy=False,
                )
            else:
                utterance = np.empty(0, dtype=np.float32)

            self._reset_state(active_stream_id=stream_id)
            future.set_result(utterance)
        except Exception as exc:
            future.set_exception(exc)

    def _handle_reset_stream(self, stream_id: int) -> None:
        self._reset_state(active_stream_id=stream_id)

    def _reset_state(self, *, active_stream_id: int) -> None:
        self._active_stream_id = active_stream_id
        self._stream_config = {}
        self._decoder = None
        self._vad.reset()
        self._speech_segments.clear()
        self._packets_received = 0
        self._bytes_received = 0
        self._accepting_audio = False

    def _set_desired_stream_id(self, stream_id: int) -> None:
        with self._stream_id_lock:
            self._desired_stream_id = stream_id

    @property
    def _desired_stream_id(self) -> int:
        with self._stream_id_lock:
            return self.__desired_stream_id

    @_desired_stream_id.setter
    def _desired_stream_id(self, value: int) -> None:
        self.__desired_stream_id = value

    def _create_opus_decoder(self, stream_config: dict[str, Any]) -> Any:
        opuslib = __import__("opuslib")
        decoder_cls = getattr(opuslib, "Decoder")
        return decoder_cls(
            _input_sample_rate(stream_config),
            _input_channels(stream_config),
        )

    def _input_frame_size(self) -> int:
        frame_ms = int(self._stream_config.get("frame_ms") or SERVER_FRAME_MS)
        return int(_input_sample_rate(self._stream_config) * (frame_ms / 1000))


def _input_sample_rate(stream_config: dict[str, Any]) -> int:
    return int(stream_config.get("sample_rate") or SERVER_SAMPLE_RATE)


def _input_channels(stream_config: dict[str, Any]) -> int:
    return int(stream_config.get("channels") or SERVER_CHANNELS)


__all__ = ["AudioStreamWorker"]
