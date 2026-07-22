"""知识库路由。"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from src.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseServiceError
from src.utils.response import success

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge_bases"])


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="知识库名称")
    desc: str = Field(
        default="",
        max_length=2000,
        strict=True,
        description="知识库描述",
    )

    def normalized_description(self) -> str:
        return self.desc.strip()


class UpdateKnowledgeBaseRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="知识库名称")
    desc: str = Field(
        default="",
        max_length=2000,
        strict=True,
        description="知识库描述",
    )

    def normalized_description(self) -> str:
        return self.desc.strip()


class DeleteKnowledgeBaseDocumentsRequest(BaseModel):
    doc_uids: list[str] = Field(default_factory=list, description="待删除文档 ID 列表")


class DocumentChunksRequest(BaseModel):
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=200, description="每页数量")


def get_knowledge_base_service(request: Request) -> KnowledgeBaseService:
    service = getattr(request.app.state, "knowledge_base_service", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="knowledge base service is disabled"
        )
    return service


@router.get("", summary="查询知识库列表")
async def list_knowledge_bases(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=10, ge=1, le=200, description="每页数量"),
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.list_knowledge_bases(
            page=page,
            page_size=page_size,
        )
        return success(
            data=_normalize_knowledge_base_list_payload(
                payload,
                page=page,
                page_size=page_size,
            )
        )
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.post("", summary="创建知识库")
async def create_knowledge_base(
    request: CreateKnowledgeBaseRequest,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        description = request.normalized_description()
        payload = await service.create_knowledge_base(
            name=request.name,
            description=description,
        )
        return success(
            data=_normalize_knowledge_base_payload(
                payload,
                fallback_description=description,
            )
        )
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.put("/{knowledge_base_id}", summary="修改知识库")
async def update_knowledge_base(
    knowledge_base_id: str,
    request: UpdateKnowledgeBaseRequest,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        description = request.normalized_description()
        payload = await service.update_knowledge_base(
            knowledge_base_id,
            name=request.name,
            description=description,
        )
        return success(
            data=_normalize_knowledge_base_payload(
                payload,
                fallback_description=description,
            )
        )
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.delete("/{knowledge_base_id}", summary="删除知识库")
async def delete_knowledge_base(
    knowledge_base_id: str,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.delete_knowledge_base(knowledge_base_id)
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.post("/{knowledge_base_id}/documents", summary="上传知识库文件")
async def upload_knowledge_base_documents(
    knowledge_base_id: str,
    files: list[UploadFile] = File(..., description="待上传文件列表"),
    parse: bool = Form(default=True, description="上传后是否立即解析"),
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.upload_documents(
            knowledge_base_id,
            files=[
                (
                    upload.filename or "unknown",
                    await upload.read(),
                    upload.content_type,
                )
                for upload in files
            ],
            parse=parse,
        )
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.get("/{knowledge_base_id}/documents", summary="查询知识库文件列表")
async def list_knowledge_base_documents(
    knowledge_base_id: str,
    name: str = Query(default="", description="文件名关键字"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=10, ge=1, le=200, description="每页数量"),
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.list_documents(
            knowledge_base_id,
            name=name,
            page=page,
            page_size=page_size,
        )
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.delete("/{knowledge_base_id}/documents", summary="删除知识库文件")
async def delete_knowledge_base_documents(
    knowledge_base_id: str,
    request: DeleteKnowledgeBaseDocumentsRequest,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.delete_documents(
            knowledge_base_id,
            doc_uids=request.doc_uids,
        )
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.post("/{knowledge_base_id}/documents/{doc_uid}/parse", summary="触发文件解析")
async def parse_knowledge_base_document(
    knowledge_base_id: str,
    doc_uid: str,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    del knowledge_base_id
    try:
        payload = await service.parse_document(doc_uid=doc_uid)
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.get("/{knowledge_base_id}/documents/{doc_uid}", summary="查询单个文件解析进度")
async def get_knowledge_base_document_detail(
    knowledge_base_id: str,
    doc_uid: str,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    try:
        payload = await service.get_document_detail(
            knowledge_base_id,
            doc_uid=doc_uid,
        )
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


@router.post(
    "/{knowledge_base_id}/documents/{doc_uid}/chunks",
    summary="查询文件解析结果",
)
async def get_knowledge_base_document_chunks(
    knowledge_base_id: str,
    doc_uid: str,
    request: DocumentChunksRequest,
    service: KnowledgeBaseService = Depends(get_knowledge_base_service),
) -> dict[str, object]:
    del knowledge_base_id
    try:
        payload = await service.get_document_chunks(
            doc_uid=doc_uid,
            page=request.page,
            page_size=request.page_size,
        )
        return success(data=_normalize_knowledge_base_payload(payload))
    except KnowledgeBaseServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=_build_error_detail(exc)
        ) from exc


def _build_error_detail(exc: KnowledgeBaseServiceError) -> dict[str, object]:
    return {
        "msg": exc.message,
        "data": exc.data,
    }


def _normalize_knowledge_base_payload(
    payload: object,
    *,
    fallback_description: str = "",
) -> object:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, (dict, list)):
            return _strip_knowledge_base_fields(
                data,
                fallback_description=fallback_description,
            )
        if data is not None:
            return data
        return _strip_knowledge_base_fields(
            payload,
            fallback_description=fallback_description,
        )
    if isinstance(payload, list):
        return [
            _normalize_knowledge_base_payload(
                item,
                fallback_description=fallback_description,
            )
            for item in payload
        ]
    return payload


def _normalize_knowledge_base_list_payload(
    payload: object,
    *,
    page: int,
    page_size: int,
) -> dict[str, object]:
    normalized = _normalize_knowledge_base_payload(payload)
    if isinstance(normalized, dict):
        raw_items = normalized.get("items")
        if not isinstance(raw_items, list):
            raw_items = normalized.get("list")
        if not isinstance(raw_items, list):
            raw_items = normalized.get("records")
        if not isinstance(raw_items, list):
            raw_items = normalized.get("data")
        if isinstance(raw_items, list):
            items = raw_items
            total = _coerce_non_negative_int(normalized.get("total"), len(items))
            resolved_page = _coerce_positive_int(normalized.get("page"), page)
            resolved_page_size = _coerce_positive_int(
                normalized.get("page_size"),
                page_size,
            )
            return {
                "items": items,
                "total": total,
                "page": resolved_page,
                "page_size": resolved_page_size,
            }
    elif isinstance(normalized, list):
        return {
            "items": normalized,
            "total": len(normalized),
            "page": page,
            "page_size": page_size,
        }

    return {
        "items": [],
        "total": 0,
        "page": page,
        "page_size": page_size,
    }


def _strip_knowledge_base_fields(
    value: object,
    *,
    fallback_description: str = "",
) -> object:
    if isinstance(value, dict):
        normalized = {
            key: _strip_knowledge_base_fields(
                item,
                fallback_description=fallback_description,
            )
            for key, item in value.items()
            if key not in {"parser_config", "id", "description"}
        }
        desc = str(normalized.get("desc") or "").strip()
        upstream_description = str(value.get("description") or "").strip()
        fallback = fallback_description.strip()
        resolved_description = desc or upstream_description or fallback
        if resolved_description:
            normalized["desc"] = resolved_description
        return normalized
    if isinstance(value, list):
        return [
            _strip_knowledge_base_fields(
                item,
                fallback_description=fallback_description,
            )
            for item in value
        ]
    return value


def _coerce_positive_int(value: object, default: int) -> int:
    result = _coerce_int(value)
    if result is None:
        return default
    return result if result >= 1 else default


def _coerce_non_negative_int(value: object, default: int) -> int:
    result = _coerce_int(value)
    if result is None:
        return default
    return result if result >= 0 else default


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["get_knowledge_base_service", "router"]
