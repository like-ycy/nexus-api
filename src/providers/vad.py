"""云端 VAD 服务。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sherpa_onnx

from src.constants import PROTOCOL_AUDIO_SAMPLE_RATE

SERVER_SAMPLE_RATE = PROTOCOL_AUDIO_SAMPLE_RATE


class SileroVadEngine:
    """服务端 Silero VAD 引擎：将连续 PCM 切分为语音段落。"""

    def __init__(self, model_path: Path) -> None:
        silero_cfg = sherpa_onnx.SileroVadModelConfig()
        silero_cfg.model = str(model_path)
        silero_cfg.threshold = 0.5
        silero_cfg.min_speech_duration = 0.25
        silero_cfg.min_silence_duration = 0.8
        silero_cfg.max_speech_duration = 20.0
        silero_cfg.window_size = 512

        vad_cfg = sherpa_onnx.VadModelConfig()
        vad_cfg.sample_rate = SERVER_SAMPLE_RATE
        vad_cfg.silero_vad = silero_cfg

        self._detector = sherpa_onnx.VoiceActivityDetector(vad_cfg)

    def accept_waveform(self, samples: np.ndarray) -> list[np.ndarray]:
        if samples.size == 0:
            return []

        self._detector.accept_waveform(samples)
        return self._drain_segments()

    def flush(self) -> list[np.ndarray]:
        self._detector.flush()
        return self._drain_segments()

    def reset(self) -> None:
        self._detector.reset()

    def _drain_segments(self) -> list[np.ndarray]:
        segments: list[np.ndarray] = []
        while not self._detector.empty():
            segment = self._detector.front
            segments.append(np.asarray(segment.samples, dtype=np.float32))
            self._detector.pop()
        return segments
