"""Shared helpers for agentbuild platform configuration."""

from __future__ import annotations

from typing import Any


def resolve_agentbuild_base_url(
    config: dict[str, Any],
    *,
    section: dict[str, Any] | None = None,
) -> str | None:
    candidates = []
    if isinstance(section, dict):
        candidates.append(section.get("base_url"))
    agentbuild_section = config.get("agentbuild")
    if isinstance(agentbuild_section, dict):
        candidates.append(agentbuild_section.get("base_url"))
    for candidate in candidates:
        text = _as_optional_str(candidate)
        if text:
            return text.rstrip("/")
    return None


def resolve_agentbuild_api_key(
    config: dict[str, Any],
    *,
    section: dict[str, Any] | None = None,
) -> str | None:
    candidates = []
    if isinstance(section, dict):
        candidates.append(section.get("api_key"))
    agentbuild_section = config.get("agentbuild")
    if isinstance(agentbuild_section, dict):
        candidates.append(agentbuild_section.get("api_key"))
    for candidate in candidates:
        text = _as_optional_str(candidate)
        if text:
            return text
    return None


def join_agentbuild_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def resolve_agentbuild_chatbot_endpoint_path(config: dict[str, Any]) -> str | None:
    agentbuild_section = config.get("agentbuild")
    if not isinstance(agentbuild_section, dict):
        return None
    chatbot_section = agentbuild_section.get("chatbot")
    if not isinstance(chatbot_section, dict):
        return None
    return _as_optional_str(chatbot_section.get("endpoint_path"))


def resolve_agentbuild_chatbot_config(config: dict[str, Any]) -> dict[str, Any] | None:
    agentbuild_section = config.get("agentbuild")
    if not isinstance(agentbuild_section, dict):
        return None
    chatbot_section = agentbuild_section.get("chatbot")
    if not isinstance(chatbot_section, dict):
        return None
    return chatbot_section


def resolve_agentbuild_knowledge_base_project_uid(config: dict[str, Any]) -> str | None:
    agentbuild_section = config.get("agentbuild")
    if not isinstance(agentbuild_section, dict):
        return None
    knowledge_base_section = agentbuild_section.get("knowledge_base")
    if not isinstance(knowledge_base_section, dict):
        return None
    return _as_optional_str(knowledge_base_section.get("project_uid"))


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
