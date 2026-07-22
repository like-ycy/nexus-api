"""统一初始化语音对话相关模块。"""

from __future__ import annotations

import threading

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.providers.asr import BaseAsrProvider, build_asr_provider
from src.providers.llm import RemoteReplyGenerator
from src.providers.tts import BaseTtsProvider, build_tts_provider
from src.utils.agentbuild import (
    resolve_agentbuild_chatbot_config,
    resolve_agentbuild_chatbot_endpoint_path,
    join_agentbuild_url,
    resolve_agentbuild_api_key,
    resolve_agentbuild_base_url,
)
from src.utils.logging import logger


@dataclass(slots=True)
class InferenceModules:
    vad_model_path: Path | None = None
    asr: BaseAsrProvider | None = None
    llm: RemoteReplyGenerator | None = None
    tts: BaseTtsProvider | None = None
    tts_model_name: str | None = None
    asr_provider_name: str | None = None
    llm_provider_name: str | None = None
    tts_provider_name: str | None = None
    inference_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def initialize_modules(
    config: dict[str, Any],
    *,
    init_vad: bool = False,
    init_asr: bool = False,
    init_llm: bool = False,
    init_tts: bool = False,
) -> InferenceModules:
    """按需初始化 VAD/ASR/LLM/TTS 组件。"""

    modules = InferenceModules()
    provider_selection_config = _require_section(config, "providers")
    modules.asr_provider_name = _get_provider_selection(
        provider_selection_config, "asr"
    )
    modules.llm_provider_name = _get_provider_selection(
        provider_selection_config, "llm"
    )
    modules.tts_provider_name = _get_provider_selection(
        provider_selection_config, "tts"
    )

    if init_vad:
        speech_config = _require_section(config, "speech")
        models_root = Path(speech_config["models_root"])
        vad_model_path = _resolve_vad_model_path(models_root / "vad")
        modules.vad_model_path = vad_model_path
        logger.info("初始化组件: vad 成功 | model={}", vad_model_path.name)

    if init_asr:
        asr_config = _require_provider_config(config, "asr", modules.asr_provider_name)
        asr_driver = str(asr_config.get("driver") or "remote_http").strip()
        modules.asr = build_asr_provider(
            driver=asr_driver,
            config=asr_config,
        )
        logger.info(
            "初始化组件: asr 成功 | selected={} driver={}",
            modules.asr_provider_name,
            asr_driver,
        )

    if init_llm:
        llm_config = _require_provider_config(config, "llm", modules.llm_provider_name)
        llm_driver = str(llm_config.get("driver") or "chatbot_sse").strip()
        if llm_driver != "chatbot_sse":
            raise ValueError(f"当前暂不支持的 LLM driver: {llm_driver}")
        chatbot_config = resolve_agentbuild_chatbot_config(config) or {}
        endpoint = _as_optional_str(llm_config.get("endpoint"))
        if not endpoint:
            base_url = resolve_agentbuild_base_url(config, section=llm_config)
            endpoint_path = (
                _as_optional_str(llm_config.get("endpoint_path"))
                or _as_optional_str(chatbot_config.get("endpoint_path"))
                or resolve_agentbuild_chatbot_endpoint_path(config)
            )
            if base_url and endpoint_path:
                endpoint = join_agentbuild_url(base_url, endpoint_path)
        modules.llm = RemoteReplyGenerator(
            endpoint=endpoint,
            api_key=resolve_agentbuild_api_key(config, section=llm_config),
            is_app_uid=True,
            request_timeout_sec=float(llm_config.get("request_timeout_sec") or 30.0),
            fallback_reply=str(
                chatbot_config.get("fallback_reply")
                or "抱歉，当前智能问答服务暂时不可用，请稍后再试。"
            ),
            streaming_enabled=bool(chatbot_config.get("streaming", True)),
            stream_answer_node_name=str(
                chatbot_config.get("stream_answer_node_name") or "智能问答"
            ),
        )
        logger.info(
            "初始化组件: llm 成功 | selected={} driver={}",
            modules.llm_provider_name,
            llm_driver,
        )

    if init_tts:
        tts_config = _require_provider_config(config, "tts", modules.tts_provider_name)
        tts_driver = str(tts_config.get("driver") or "index_stream").strip()
        modules.tts = build_tts_provider(
            driver=tts_driver,
            config=tts_config,
        )
        modules.tts_model_name = tts_driver
        logger.info(
            "初始化组件: tts 成功 | selected={} driver={}",
            modules.tts_provider_name,
            tts_driver,
        )

    return modules


def _get_provider_selection(provider_selection_config: dict[str, Any], key: str) -> str:
    value = str(provider_selection_config.get(key) or "").strip()
    if not value:
        raise ValueError(f"providers.{key} 未配置")
    return value


def _require_provider_config(
    config: dict[str, Any],
    section_name: str,
    provider_name: str | None,
) -> dict[str, Any]:
    if not provider_name:
        raise ValueError(f"providers.{section_name} 未配置")
    section = _require_section(config, section_name)
    provider_config = section.get(provider_name)
    if not isinstance(provider_config, dict):
        raise ValueError(f"未找到 {section_name}.{provider_name} 配置")
    return provider_config


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


def _resolve_vad_model_path(vad_root: Path) -> Path:
    for candidate in (
        vad_root / "silero_vad.onnx",
        vad_root / "silero_vad.int8.onnx",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"未找到 Silero VAD 模型: {vad_root}")


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["InferenceModules", "initialize_modules"]
