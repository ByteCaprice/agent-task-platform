"""HTTP middleware for request/response access logs with secret redaction."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import Response

from framework.observability import (
    REQUEST_TRACE_HEADER,
    install_request_trace_log_record_factory,
    log_json,
    new_request_trace_id,
    redact_for_log,
    reset_request_trace_id,
    safe_url,
    set_request_trace_id,
)

logger = logging.getLogger("agent_task_platform.http")


def install_request_logging_middleware(
    app: FastAPI,
    *,
    max_body_chars: int = 4096,
    settings: dict[str, Any] | None = None,
) -> None:
    install_request_trace_log_record_factory()

    @app.middleware("http")
    async def _request_logging_middleware(request: Request, call_next):
        logging_settings = settings.get("logging", {}) if settings else {}
        body_limit = int(logging_settings.get("max_body_chars", max_body_chars))
        log_payloads = bool(logging_settings.get("log_payloads", False))
        request_trace_id = request.headers.get(REQUEST_TRACE_HEADER) or new_request_trace_id()
        token = set_request_trace_id(request_trace_id)
        request.state.request_trace_id = request_trace_id
        started = time.perf_counter()
        try:
            body = await request.body() if log_payloads else b""
            request_info = _request_info(
                request, body, request_trace_id, max_body_chars=body_limit, include_payload=log_payloads
            )
            logger.info("http request: %s", log_json(request_info, max_chars=body_limit))

            try:
                response = await call_next(request)
            except Exception:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.exception(
                    "http response: %s",
                    log_json(
                        {
                            "request_trace_id": request_trace_id,
                            "method": request.method,
                            "path": request.url.path,
                            "status": 500,
                            "elapsed_ms": elapsed_ms,
                        },
                        max_chars=body_limit,
                    ),
                )
                raise

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            response_info = _response_info(request, response, elapsed_ms, request_trace_id)
            logger.info("http response: %s", log_json(response_info, max_chars=body_limit))
            response.headers[REQUEST_TRACE_HEADER] = request_trace_id
            return response
        finally:
            reset_request_trace_id(token)


def _request_info(
    request: Request,
    body: bytes,
    request_trace_id: str,
    *,
    max_body_chars: int,
    include_payload: bool = False,
) -> dict[str, Any]:
    info = {
        "request_trace_id": request_trace_id,
        "method": request.method,
        "path": request.url.path,
        "query_keys": sorted(request.query_params.keys()),
        "headers": redact_for_log(
            {
                "content-type": request.headers.get("content-type"),
                "x-api-key": request.headers.get("x-api-key"),
                "authorization": request.headers.get("authorization"),
            }
        ),
    }
    if include_payload:
        info["body"] = _limit_body(
            _parse_body(body, request.headers.get("content-type", "")), max_body_chars=max_body_chars
        )
    return info


def _response_info(
    request: Request,
    response: Response,
    elapsed_ms: int,
    request_trace_id: str,
) -> dict[str, Any]:
    return {
        "request_trace_id": request_trace_id,
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "content_type": response.headers.get("content-type"),
    }


def _parse_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    if "json" in content_type.lower():
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    text = body.decode("utf-8", errors="replace")
    if "://" in text:
        return safe_url(text)
    return text


def _limit_body(value: Any, *, max_body_chars: int) -> Any:
    text = log_json(value, max_chars=max_body_chars)
    if len(text) <= max_body_chars:
        return value
    return f"{text[:max_body_chars]}...<truncated>"
