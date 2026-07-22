from __future__ import annotations

import io
import os
import time
import wave

from pathlib import Path
from typing import Any

import numpy as np

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from funasr import AutoModel
from loguru import logger

SERVER_SAMPLE_RATE = 16000
SERVICE_NAME = "nexus-asr-service"
DEFAULT_MODEL_NAME = "funasr-paraformer-zh"
DEFAULT_MODEL_DIR = f"/models/asr/{DEFAULT_MODEL_NAME}"


class FunasrBackend:
    def __init__(self, model_dir: Path, *, model_name: str, device: str) -> None:
        self.model_dir = model_dir
        self.model_name = model_name
        self.device = device
        self._model = AutoModel(
            model=str(model_dir),
            device=device,
            disable_update=True,
        )
        logger.info(
            "ASR backend loaded | backend=funasr model={} dir={} device={}",
            model_name,
            model_dir,
            device,
        )

    def metadata(self) -> dict[str, object]:
        return {
            "service": SERVICE_NAME,
            "backend": "funasr",
            "model": self.model_name,
            "model_dir": str(self.model_dir),
            "device": self.device,
            "sample_rate": SERVER_SAMPLE_RATE,
            "features": {
                "punctuation": False,
                "itn": False,
                "hotwords": False,
                "streaming": False,
            },
        }

    def transcribe(self, samples: np.ndarray) -> str:
        result = self._model.generate(
            input=samples,
            sample_rate=SERVER_SAMPLE_RATE,
            batch_size_s=60,
        )
        return _extract_text_recursive(result).strip()


def create_app() -> FastAPI:
    model_name = os.getenv("ASR_MODEL_NAME", DEFAULT_MODEL_NAME).strip()
    model_dir = Path(os.getenv("ASR_MODEL_DIR", DEFAULT_MODEL_DIR)).resolve()
    device = os.getenv("ASR_DEVICE", "cpu").strip() or "cpu"

    if not model_dir.exists():
        raise RuntimeError(f"ASR model dir does not exist: {model_dir}")

    backend = FunasrBackend(model_dir, model_name=model_name, device=device)
    app = FastAPI(title=SERVICE_NAME)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", **backend.metadata()})

    @app.get("/metadata")
    async def metadata() -> JSONResponse:
        return JSONResponse(backend.metadata())

    @app.post("/asr")
    async def asr(file: UploadFile = File(...)) -> JSONResponse:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="empty audio file")

        samples, sample_rate = _read_wav_bytes(payload)
        if sample_rate != SERVER_SAMPLE_RATE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported sample rate: {sample_rate}, "
                    f"expected {SERVER_SAMPLE_RATE}"
                ),
            )

        started_at = time.perf_counter()
        text = backend.transcribe(samples)
        latency_ms = (time.perf_counter() - started_at) * 1000
        return JSONResponse(
            {
                "text": text,
                "backend": "funasr",
                "model": backend.model_name,
                "device": backend.device,
                "sample_rate": SERVER_SAMPLE_RATE,
                "latency_ms": latency_ms,
            }
        )

    return app


def _read_wav_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            raw_frames = wav_file.readframes(frame_count)
    except wave.Error as exc:
        raise HTTPException(status_code=400, detail=f"invalid wav file: {exc}") from exc

    if channels != 1:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported channel count: {channels}",
        )
    if sample_width != 2:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported sample width: {sample_width}",
        )

    samples = np.frombuffer(raw_frames, dtype="<i2").astype(np.float32)
    return np.clip(samples / 32768.0, -1.0, 1.0), sample_rate


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


app = create_app()
