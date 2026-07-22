"""项目配置加载入口。"""

from __future__ import annotations

import json
import os

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.utils.path import get_project_paths

import tomli

_PROJECT_PATHS = get_project_paths()
_ENV_PREFIX = "NEXUS_"
_ENV_NESTED_DELIMITER = "__"
_DEFAULT_CONFIG_PATH = _PROJECT_PATHS.config_dir / "env.test.toml"
_DEFAULT_PERCEPT_API_ENDPOINT = "http://192.168.21.138:18080/embodied_api"
_DEFAULT_PERCEPT_API_KEY = "nexus-percept-internal-key"
_PATH_FIELDS = (
    ("logging", "log_path"),
    ("speech", "models_root"),
)
_DEFAULT_CONFIG: dict[str, Any] = {
    "logging": {
        "level": "INFO",
        "log_path": None,
    },
    "websocket": {
        "host": "0.0.0.0",
        "port": 8765,
        "max_message_size": 10 * 1024 * 1024,
    },
    "http": {
        "host": "0.0.0.0",
        "port": 8080,
    },
    "speech": {
        "models_root": "models",
    },
    "providers": {
        "asr": "primary",
        "llm": "chatbot_api",
        "tts": "remote_service",
    },
    "asr": {
        "primary": {
            "driver": "remote_http",
            "endpoint": "http://127.0.0.1:18081/asr",
            "request_timeout_sec": 20.0,
            "audio_field_name": "file",
            "audio_filename": "audio.wav",
            "response_text_path": "text",
            "headers": {},
            "form_fields": {},
        },
    },
    "llm": {
        "chatbot_api": {
            "driver": "chatbot_sse",
            "request_timeout_sec": 30.0,
        },
    },
    "tts": {
        "remote_service": {
            "driver": "remote_http_stream",
            "stream_url": "http://127.0.0.1:18083/tts/stream",
            "voice": "default",
            "request_timeout_sec": 30.0,
        },
    },
    "agentbuild": {
        "base_url": None,
        "api_key": None,
        "chatbot": {
            "endpoint_path": None,
            "streaming": True,
            "stream_answer_node_name": "智能问答",
            "fallback_reply": "抱歉，当前智能问答服务暂时不可用，请稍后再试。",
        },
        "knowledge_base": {
            "project_uid": None,
        },
    },
    "database": {
        "dsn": None,
        "min_pool_size": 1,
        "max_pool_size": 5,
        "command_timeout_sec": 10.0,
    },
    "database_embodied": {
        "dsn": None,
        "min_pool_size": 1,
        "max_pool_size": 5,
        "command_timeout_sec": 10.0,
    },
    "conversation": {
        "session_limit": 20,
        "turn_limit": 50,
        "queue_size": 1000,
    },
    "percept-api": {
        "endpoint": _DEFAULT_PERCEPT_API_ENDPOINT,
        "x-api-key": _DEFAULT_PERCEPT_API_KEY,
    },
    "live_camera": {
        "srs_webrtc_base_url": "http://192.168.21.138:1985",
        "srs_http_base_url": "http://192.168.21.138:18080",
        "srs_app": "live",
    },
    "knowledge_base": {
        "base_url": None,
        "project_uid": None,
        "api_key": None,
        "request_timeout_sec": 30.0,
    },
}
_config_cache: dict[str, Any] | None = None


def get_config_path() -> Path:
    """返回当前主配置文件路径。"""
    configured = os.getenv(f"{_ENV_PREFIX}CONFIG_PATH", "").strip()
    if not configured:
        return _DEFAULT_CONFIG_PATH

    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = (_PROJECT_PATHS.project_root / path).resolve()
    return path


def read_config(config_path: str | Path) -> dict[str, Any]:
    """读取 TOML 配置文件。"""
    path = Path(config_path)
    with path.open("rb") as file:
        content = tomli.load(file)
    if not isinstance(content, dict):
        raise ValueError(f"配置文件格式不正确: {path}")
    return content


def merge_configs(
    base_config: Mapping[str, Any],
    override_config: Mapping[str, Any],
) -> dict[str, Any]:
    """递归合并配置，override_config 优先级更高。"""
    merged = dict(base_config)

    for key, value in override_config.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_configs(current, value)
            continue
        merged[key] = value

    return merged


def apply_env_overrides(
    config: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """将环境变量覆盖到配置树中。"""
    source = os.environ if environ is None else environ
    overridden = deepcopy(config)

    for key, raw_value in source.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        if key == f"{_ENV_PREFIX}CONFIG_PATH":
            continue

        path = [
            segment.strip().lower()
            for segment in key[len(_ENV_PREFIX) :].split(_ENV_NESTED_DELIMITER)
            if segment.strip()
        ]
        if not path:
            continue

        _set_nested_value(overridden, path, _parse_env_value(raw_value))

    return overridden


def load_config(*, force_reload: bool = False) -> dict[str, Any]:
    """加载配置并缓存。"""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    config_path = get_config_path()
    if not config_path.is_file():
        raise FileNotFoundError(f"未找到配置文件: {config_path}")

    merged = merge_configs(_DEFAULT_CONFIG, read_config(config_path))
    merged = apply_env_overrides(merged)
    _config_cache = _normalize_config(merged)
    return _config_cache


def reload_config() -> dict[str, Any]:
    """强制重新加载配置。"""
    return load_config(force_reload=True)


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(config)

    for section_name, defaults in _DEFAULT_CONFIG.items():
        section = normalized.get(section_name)
        if isinstance(section, Mapping):
            normalized[section_name] = merge_configs(defaults, section)
            continue
        normalized[section_name] = deepcopy(defaults)

    for section_name, key in _PATH_FIELDS:
        section = normalized.get(section_name)
        if not isinstance(section, dict):
            continue
        section[key] = _resolve_project_path(section.get(key))

    return normalized


def _resolve_project_path(value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        path = value.expanduser()
    else:
        text = str(value).strip()
        if not text:
            return None
        path = Path(text).expanduser()

    if path.is_absolute():
        return path.resolve()
    return (_PROJECT_PATHS.project_root / path).resolve()


def _set_nested_value(config: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = config
    for segment in path[:-1]:
        child = cursor.get(segment)
        if not isinstance(child, dict):
            child = {}
            cursor[segment] = child
        cursor = child
    cursor[path[-1]] = value


def _parse_env_value(raw_value: str) -> Any:
    text = raw_value.strip()
    if not text:
        return ""

    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw_value


__all__ = [
    "apply_env_overrides",
    "get_config_path",
    "load_config",
    "merge_configs",
    "read_config",
    "reload_config",
]
