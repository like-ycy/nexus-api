"""统一 HTTP 响应格式。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ResponseModel(BaseModel):
    code: int = 200
    msg: str = "success"
    data: Any = None


class Code:
    SUCCESS = 200
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    VALIDATION_ERROR = 422
    INTERNAL_ERROR = 500
    SERVICE_UNAVAILABLE = 503


def success(data: Any = None, msg: str = "success") -> dict[str, Any]:
    return ResponseModel(code=Code.SUCCESS, msg=msg, data=data).model_dump()


def error(
    code: int = Code.BAD_REQUEST,
    msg: str = "error",
    data: Any = None,
) -> dict[str, Any]:
    return ResponseModel(code=code, msg=msg, data=data).model_dump()


__all__ = ["Code", "ResponseModel", "error", "success"]
