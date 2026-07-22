"""知识库代理服务，封装 agentbuild 平台接口。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx

from src.utils.agentbuild import resolve_agentbuild_api_key, resolve_agentbuild_base_url
from src.utils.agentbuild import resolve_agentbuild_knowledge_base_project_uid


class KnowledgeBaseServiceError(RuntimeError):
    """知识库服务调用失败。"""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        data: object = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class _KnowledgeBaseConfig:
    base_url: str
    project_uid: str
    request_timeout_sec: float
    api_key: str


_DEFAULT_PARSER_CONFIG: dict[str, Any] = {
    "mode": "advanced",
    "embd_version": 2,
    "advanced_pdf": {
        "parser_method": "complex",
        "parser_parameter": {
            "remove_watermark": False,
            "remove_redseal": False,
            "text_model_name": "dots-ocr",
            "check_llm": "466e03aa62e6431dbb60f6c4ea192668",
            "image_model_name": "InternVL3-38B-AWQ",
            "complex_table": False,
            "hide_header": True,
            "hide_footer": True,
            "image_recog_context": True,
        },
        "chunk_method": "standard",
        "chunk_parameter": {
            "chunk_token_num": 512,
            "chunk_overlap": 0.1,
        },
    },
    "advanced_docx": {
        "parser_method": "standard",
        "parser_parameter": {
            "image_model_name": "InternVL3-38B-AWQ",
            "image_recog_context": True,
        },
        "chunk_method": "standard",
        "chunk_parameter": {
            "chunk_token_num": 512,
            "chunk_overlap": 0.1,
        },
    },
    "advanced_pptx": {
        "parser_method": "picture",
        "parser_parameter": {
            "image_model_name": "InternVL3-38B-AWQ",
        },
        "chunk_method": "standard",
        "chunk_parameter": {
            "chunk_token_num": 512,
            "chunk_overlap": 0.1,
        },
    },
    "advanced_xlsx": {
        "parser_method": "standard",
        "parser_parameter": {
            "image_model_name": "InternVL3-38B-AWQ",
            "image_recog_context": True,
        },
        "chunk_method": "table_row",
        "chunk_parameter": {
            "add_create_time": False,
        },
    },
    "advanced_csv": {
        "parser_method": "standard",
        "parser_parameter": {
            "image_model_name": "",
        },
        "chunk_method": "table_row",
        "chunk_parameter": {
            "add_create_time": False,
        },
    },
    "advanced_json": {
        "parser_method": "plaintext",
        "chunk_method": "json_object",
        "chunk_parameter": {
            "add_create_time": False,
        },
    },
    "advanced_image": {
        "parser_method": "picture",
        "parser_parameter": {
            "image_model_name": "InternVL3-38B-AWQ",
        },
        "chunk_method": "standard",
        "chunk_parameter": {
            "chunk_token_num": 512,
            "chunk_overlap": 0.1,
        },
    },
    "advanced_txt": {
        "parser_method": "plaintext",
        "chunk_method": "standard",
        "chunk_parameter": {
            "chunk_token_num": 512,
            "chunk_overlap": 0.1,
        },
    },
    "multi_embd_id": "BAAI/bge-m3",
    "image_embading": "BAAI/BGE-VL-large",
}


class KnowledgeBaseService:
    """当前平台知识库能力，底层复用 agentbuild 平台。"""

    def __init__(self, config: dict[str, Any]) -> None:
        knowledge_base_config = _require_section(config, "knowledge_base")
        base_url = _as_optional_str(
            knowledge_base_config.get("base_url")
        ) or resolve_agentbuild_base_url(config, section=knowledge_base_config)
        project_uid = _as_required_str(
            knowledge_base_config.get("project_uid")
            or resolve_agentbuild_knowledge_base_project_uid(config),
            key="knowledge_base.project_uid",
        )
        api_key = _as_optional_str(
            knowledge_base_config.get("api_key")
        ) or resolve_agentbuild_api_key(
            config,
            section=knowledge_base_config,
        )
        if not base_url:
            raise ValueError("missing required config: agentbuild.base_url")
        if not api_key:
            raise ValueError("missing required config: agentbuild.api_key")
        self._config = _KnowledgeBaseConfig(
            base_url=base_url.rstrip("/"),
            project_uid=project_uid,
            request_timeout_sec=float(
                knowledge_base_config.get("request_timeout_sec") or 30.0
            ),
            api_key=api_key,
        )
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        normalized_name = name.strip()
        normalized_description = description.strip()
        await self._ensure_unique_knowledge_base_name(normalized_name)
        payload = await self._request_json(
            method="POST",
            url=f"{self._config.base_url}/rag/kb/create",
            json_body={
                "name": normalized_name,
                "desc": normalized_description,
                "description": normalized_description,
                "parser_config": _build_default_parser_config(),
            },
            params={"project_uid": self._config.project_uid},
        )
        return _fill_response_description(payload, normalized_description)

    async def list_knowledge_bases(
        self,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="GET",
            url=f"{self._config.base_url}/rag/kb",
            params={
                "project_uid": self._config.project_uid,
                "page": page,
                "page_size": page_size,
            },
        )

    async def update_knowledge_base(
        self,
        knowledge_base_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        normalized_name = name.strip()
        normalized_description = description.strip()
        await self._ensure_unique_knowledge_base_name(
            normalized_name,
            exclude_knowledge_base_id=knowledge_base_id,
        )
        payload = await self._request_json(
            method="PUT",
            url=f"{self._config.base_url}/rag/kb/edit",
            json_body={
                "uid": knowledge_base_id,
                "name": normalized_name,
                "desc": normalized_description,
                "description": normalized_description,
                "parser_config": _build_default_parser_config(),
            },
            params={"project_uid": self._config.project_uid},
        )
        return _fill_response_description(payload, normalized_description)

    async def delete_knowledge_base(self, knowledge_base_id: str) -> dict[str, Any]:
        return await self._request_json(
            method="DELETE",
            url=f"{self._config.base_url}/rag/kb/delete",
            json_body={"uid": knowledge_base_id},
            params={"project_uid": self._config.project_uid},
        )

    async def upload_documents(
        self,
        knowledge_base_id: str,
        *,
        files: list[tuple[str, bytes, str | None]],
        parse: bool,
    ) -> dict[str, Any]:
        multipart_files = [
            (
                "files",
                (filename, payload, content_type or "application/octet-stream"),
            )
            for filename, payload, content_type in files
        ]
        form_data = {
            "kb_uid": knowledge_base_id,
            "parse": "TRUE" if parse else "FALSE",
        }
        return await self._request_json(
            method="POST",
            url=f"{self._config.base_url}/common/doc/upload",
            files=multipart_files,
            data=form_data,
            params={"project_uid": self._config.project_uid},
        )

    async def parse_document(
        self,
        *,
        doc_uid: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            url=f"{self._config.base_url}/rag/doc/parse",
            params={
                "doc_uid": doc_uid,
                "project_uid": self._config.project_uid,
            },
        )

    async def delete_documents(
        self,
        knowledge_base_id: str,
        *,
        doc_uids: list[str],
    ) -> dict[str, Any]:
        return await self._request_json(
            method="DELETE",
            url=f"{self._config.base_url}/rag/kb/docs",
            json_body={
                "uid": knowledge_base_id,
                "doc_list": doc_uids,
            },
            params={"project_uid": self._config.project_uid},
        )

    async def list_documents(
        self,
        knowledge_base_id: str,
        *,
        name: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="GET",
            url=f"{self._config.base_url}/rag/kb/docs",
            params={
                "kb_uid": knowledge_base_id,
                "name": name,
                "page": page,
                "page_size": page_size,
                "project_uid": self._config.project_uid,
            },
        )

    async def get_document_detail(
        self,
        knowledge_base_id: str,
        *,
        doc_uid: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="GET",
            url=f"{self._config.base_url}/rag/kb/doc/detail",
            params={
                "project_uid": self._config.project_uid,
                "kb_uid": knowledge_base_id,
                "doc_uid": doc_uid,
            },
        )

    async def get_document_chunks(
        self,
        *,
        doc_uid: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            url=f"{self._config.base_url}/rag/doc/chunk/list",
            json_body={
                "doc_uid": doc_uid,
                "page": page,
                "page_size": page_size,
            },
            params={"project_uid": self._config.project_uid},
        )

    async def _request_json(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self._config.request_timeout_sec, connect=5.0)
        try:
            client = self._ensure_client(timeout)
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers=_build_headers(self._config.api_key),
            )
        except httpx.RequestError as exc:
            raise KnowledgeBaseServiceError(
                status_code=502,
                message=f"knowledge base upstream request failed: {exc}",
            ) from exc
        except Exception as exc:
            raise KnowledgeBaseServiceError(
                status_code=502,
                message=f"knowledge base request failed: {type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code >= 400:
            detail = _parse_response_payload(response)
            message = (
                _extract_error_message(detail)
                or f"knowledge base upstream returned {response.status_code}"
            )
            raise KnowledgeBaseServiceError(
                status_code=response.status_code,
                message=message,
                data=detail,
            )

        payload = _parse_response_payload(response)
        if not isinstance(payload, dict):
            raise KnowledgeBaseServiceError(
                status_code=502,
                message="knowledge base upstream returned non-json payload",
                data={"text": response.text},
            )
        return payload

    def _ensure_client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def _ensure_unique_knowledge_base_name(
        self,
        name: str,
        *,
        exclude_knowledge_base_id: str | None = None,
    ) -> None:
        normalized_name = _normalize_name(name)
        if not normalized_name:
            raise KnowledgeBaseServiceError(
                status_code=400,
                message="knowledge base name is required",
            )
        exclude_id = str(exclude_knowledge_base_id or "").strip()
        for item in await self._list_all_knowledge_base_items():
            item_name = _normalize_name(item.get("name"))
            if item_name != normalized_name:
                continue
            item_id = str(item.get("uid") or item.get("id") or "").strip()
            if exclude_id and item_id == exclude_id:
                continue
            raise KnowledgeBaseServiceError(
                status_code=409,
                message="知识库名称重复，不允许重复",
                data={"name": name},
            )

    async def _list_all_knowledge_base_items(self) -> list[dict[str, Any]]:
        page = 1
        page_size = 200
        items: list[dict[str, Any]] = []
        while True:
            payload = await self.list_knowledge_bases(page=page, page_size=page_size)
            page_items = _extract_knowledge_base_items(payload)
            items.extend(page_items)
            if len(page_items) < page_size:
                return items
            page += 1


def build_knowledge_base_service(config: dict[str, Any]) -> KnowledgeBaseService | None:
    section = config.get("knowledge_base")
    if not isinstance(section, dict):
        return None
    base_url = _as_optional_str(section.get("base_url")) or resolve_agentbuild_base_url(
        config,
        section=section,
    )
    project_uid = _as_optional_str(
        section.get("project_uid")
    ) or resolve_agentbuild_knowledge_base_project_uid(config)
    if not base_url or not project_uid:
        return None
    return KnowledgeBaseService(config)


def _build_default_parser_config() -> dict[str, Any]:
    return deepcopy(_DEFAULT_PARSER_CONFIG)


def _build_headers(api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _parse_response_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


def _extract_error_message(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_knowledge_base_items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "list", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    for key in ("items", "list", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _fill_response_description(
    payload: dict[str, Any], description: str
) -> dict[str, Any]:
    if not description:
        return payload

    cloned = deepcopy(payload)
    target = cloned.get("data") if isinstance(cloned.get("data"), dict) else cloned
    if not isinstance(target, dict):
        return cloned

    if not str(target.get("desc") or "").strip():
        target["desc"] = description
    if not str(target.get("description") or "").strip():
        target["description"] = description
    return cloned


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _require_section(config: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = config.get(section_name)
    if not isinstance(section, dict):
        raise ValueError(f"配置段缺失或格式不正确: {section_name}")
    return section


def _as_required_str(value: object, *, key: str) -> str:
    text = _as_optional_str(value)
    if text is None:
        raise ValueError(f"missing required config: {key}")
    return text


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "KnowledgeBaseService",
    "KnowledgeBaseServiceError",
    "build_knowledge_base_service",
]
