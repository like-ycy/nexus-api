"""Structured per-turn trace logging."""

from __future__ import annotations

import json
from typing import Any

from src.utils.logging import logger


def log_turn_trace(
    *,
    stage: str,
    event: str,
    session_id: str | None = None,
    **fields: object,
) -> None:
    parts = [
        "TURN_TRACE",
        f"stage={stage}",
        f"event={event}",
        f"session_id={session_id or '-'}",
    ]
    for key, value in fields.items():
        parts.append(f"{key}={_format_value(value)}")
    logger.info(" | ".join(parts))


def _format_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.replace("\r", "\\r").replace("\n", "\\n")
    return json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"))


def _json_safe(value: object) -> Any:
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value
