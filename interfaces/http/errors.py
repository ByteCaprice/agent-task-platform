"""Unified API error envelope.

All non-runtime-protocol API errors return a consistent JSON structure:

    {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "human-readable message",
            "details": { ... }  // optional, no internal stack traces
        }
    }
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

LANGUAGE_HEADER = "Sr-Lang"

_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        "BAD_REQUEST": "请求参数无效",
        "UNAUTHORIZED": "登录状态无效或已过期",
        "FORBIDDEN": "无权执行此操作",
        "NOT_FOUND": "请求的资源不存在",
        "CONFLICT": "当前状态不允许此操作",
        "RATE_LIMITED": "请求过于频繁，请稍后重试",
        "VALIDATION_ERROR": "请求参数校验失败",
        "REGISTRY_ERROR": "Agent 或工具注册配置无效",
        "INTERNAL_ERROR": "服务暂时不可用，请稍后重试",
        "BAD_GATEWAY": "上游服务响应异常",
        "SERVICE_UNAVAILABLE": "服务暂时不可用",
        "AR1001": "Run 不存在",
        "AI1002": "无权访问 AI Platform",
        "AI1003": "需要 Agent Runtime 操作权限",
    },
    "en-US": {
        "BAD_REQUEST": "Invalid request",
        "UNAUTHORIZED": "Your session is invalid or has expired",
        "FORBIDDEN": "You do not have permission to perform this action",
        "NOT_FOUND": "The requested resource was not found",
        "CONFLICT": "This action is not allowed in the current state",
        "RATE_LIMITED": "Too many requests. Please try again later",
        "VALIDATION_ERROR": "Request validation failed",
        "REGISTRY_ERROR": "The agent or tool registry configuration is invalid",
        "INTERNAL_ERROR": "The service is temporarily unavailable. Please try again later",
        "BAD_GATEWAY": "The upstream service returned an invalid response",
        "SERVICE_UNAVAILABLE": "The service is temporarily unavailable",
        "AR1001": "Run not found",
        "AI1002": "AI Platform access denied",
        "AI1003": "Runtime operation permission required",
    },
}


class APIError(Exception):
    """Application-level error with code and optional details."""

    def __init__(
        self,
        *,
        code: str = "INTERNAL_ERROR",
        message: str = "An internal error occurred",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


def _error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def _request_locale(request: Request) -> str:
    value = request.headers.get(LANGUAGE_HEADER, "en-US").replace("_", "-").lower()
    return "zh-CN" if value == "zh-cn" else "en-US"


def _localized_message(request: Request, code: str, fallback: str) -> str:
    return _ERROR_MESSAGES[_request_locale(request)].get(code, fallback)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    message = _localized_message(request, exc.code, exc.message)
    logger.warning("api error: code=%s status=%s message=%s", exc.code, exc.status_code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, message, exc.details),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        502: "BAD_GATEWAY",
        503: "SERVICE_UNAVAILABLE",
    }
    code = code_map.get(exc.status_code, "ERROR")
    fallback = str(exc.detail) if exc.detail else "Error"
    message = _localized_message(request, code, fallback)
    logger.warning("http error: code=%s status=%s message=%s", code, exc.status_code, message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, message),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("request validation failed: errors=%s", exc.errors())
    return JSONResponse(
        status_code=422,
        content=_error_body(
            "VALIDATION_ERROR",
            _localized_message(request, "VALIDATION_ERROR", "Request validation failed"),
            {"errors": exc.errors()},
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all that never leaks internal details."""
    logger.exception("unhandled api exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content=_error_body(
            "INTERNAL_ERROR",
            _localized_message(request, "INTERNAL_ERROR", "An internal error occurred"),
        ),
    )
