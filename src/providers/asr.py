"""云端 ASR 服务。"""

from __future__ import annotations

from abc import ABC, abstractmethod
import io
import re
import wave
from typing import Any

import httpx
import numpy as np

from src.constants import PROTOCOL_AUDIO_SAMPLE_RATE
from src.utils.logging import logger

SERVER_SAMPLE_RATE = PROTOCOL_AUDIO_SAMPLE_RATE


class BaseAsrProvider(ABC):
    """ASR provider 统一接口。"""

    driver: str

    @abstractmethod
    def transcribe(self, samples: np.ndarray) -> str:
        """把当前一轮完整音频转成文本。"""


class RemoteHttpAsrProvider(BaseAsrProvider):
    """通用远端 HTTP ASR provider。"""

    driver = "remote_http"

    def __init__(self, config: dict[str, Any]) -> None:
        self._endpoint = _require_text(config, "endpoint")
        self._timeout_sec = float(config.get("request_timeout_sec") or 20.0)
        self._audio_field_name = (
            str(config.get("audio_field_name") or "file").strip() or "file"
        )
        self._audio_filename = (
            str(config.get("audio_filename") or "audio.wav").strip() or "audio.wav"
        )
        self._response_text_path = str(config.get("response_text_path") or "").strip()
        raw_headers = config.get("headers")
        raw_form_fields = config.get("form_fields")
        self._headers = (
            {
                str(key): str(value)
                for key, value in raw_headers.items()
                if value is not None
            }
            if isinstance(raw_headers, dict)
            else {}
        )
        self._form_fields = (
            {
                str(key): str(value)
                for key, value in raw_form_fields.items()
                if value is not None
            }
            if isinstance(raw_form_fields, dict)
            else {}
        )
        logger.info(
            "ASR 已加载 | driver={} endpoint={} audio_field={} timeout_sec={}",
            self.driver,
            self._endpoint,
            self._audio_field_name,
            self._timeout_sec,
        )

    def transcribe(self, samples: np.ndarray) -> str:
        wav_bytes = build_wav_bytes(samples, sample_rate=SERVER_SAMPLE_RATE)
        with httpx.Client(timeout=self._timeout_sec) as client:
            response = client.post(
                self._endpoint,
                headers=self._headers or None,
                data=self._form_fields or None,
                files={
                    self._audio_field_name: (
                        self._audio_filename,
                        wav_bytes,
                        "audio/wav",
                    )
                },
            )
            response.raise_for_status()

        payload = response.json()
        text = extract_asr_text(
            payload, response_text_path=self._response_text_path
        ).strip()
        text = normalize_asr_text(text)
        return text


def build_asr_provider(
    *,
    driver: str,
    config: dict[str, Any],
) -> BaseAsrProvider:
    normalized_driver = driver.strip()
    if normalized_driver == RemoteHttpAsrProvider.driver:
        return RemoteHttpAsrProvider(config)
    raise ValueError(f"当前暂不支持的 ASR driver: {normalized_driver}")


def build_wav_bytes(samples: np.ndarray, *, sample_rate: int) -> bytes:
    pcm16 = (
        np.rint(np.clip(samples, -1.0, 32767.0 / 32768.0) * 32767.0)
        .astype(np.int16)
        .tobytes()
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16)
    return buffer.getvalue()


def extract_asr_text(
    payload: Any,
    *,
    response_text_path: str = "",
) -> str:
    if response_text_path:
        text = _extract_by_path(payload, response_text_path)
        if isinstance(text, str):
            return text
    return _extract_text_recursive(payload)


def normalize_asr_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<\|[^|]+?\|>", "", text).strip()


def _extract_text_recursive(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in (
            "text",
            "transcript",
            "sentence",
            "result",
            "value",
            "content",
            "asr_result",
        ):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        for value in payload.values():
            text = _extract_text_recursive(value)
            if text:
                return text
        return ""
    if isinstance(payload, (list, tuple)):
        for item in payload:
            text = _extract_text_recursive(item)
            if text:
                return text
        return ""
    return ""


def _extract_by_path(payload: Any, path: str) -> Any:
    current = payload
    for segment in path.split("."):
        if not segment:
            continue
        if isinstance(current, dict):
            current = current.get(segment)
            continue
        return None
    return current


def _require_text(config: dict[str, Any], key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ValueError(f"ASR 配置缺少字段: {key}")
    return value


SpeechRecognizer = RemoteHttpAsrProvider

__all__ = [
    "BaseAsrProvider",
    "RemoteHttpAsrProvider",
    "SpeechRecognizer",
    "build_asr_provider",
    "build_wav_bytes",
    "extract_asr_text",
    "normalize_asr_text",
]
