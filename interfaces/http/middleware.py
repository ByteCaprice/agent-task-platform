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


def install_request_logging_middleware(app: FastAPI, *, max_body_chars: int = 4096) -> None:
    install_request_trace_log_record_factory()

    @app.middleware("http")
    async def _request_logging_middleware(request: Request, call_next):
        request_trace_id = request.headers.get(REQUEST_TRACE_HEADER) or new_request_trace_id()
        token = set_request_trace_id(request_trace_id)
        request.state.request_trace_id = request_trace_id
        started = time.perf_counter()
        try:
            body = await request.body()
            request_info = _request_info(request, body, request_trace_id, max_body_chars=max_body_chars)
            logger.info("http request: %s", log_json(request_info, max_chars=max_body_chars))

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": body, "more_body": False}

            replayed_request = Request(request.scope, receive)
            try:
                response = await call_next(replayed_request)
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
                        max_chars=max_body_chars,
                    ),
                )
                raise

            chunks = [chunk async for chunk in response.body_iterator]
            response_body = b"".join(chunks)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            response_info = _response_info(
                request,
                response,
                response_body,
                elapsed_ms,
                request_trace_id,
                max_body_chars=max_body_chars,
            )
            logger.info("http response: %s", log_json(response_info, max_chars=max_body_chars))

            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers[REQUEST_TRACE_HEADER] = request_trace_id
            return Response(
                content=response_body,
                status_code=response.status_code,
                headers=headers,
                media_type=response.media_type,
                background=response.background,
            )
        finally:
            reset_request_trace_id(token)


def _request_info(request: Request, body: bytes, request_trace_id: str, *, max_body_chars: int) -> dict[str, Any]:
    parsed_body = _parse_body(body, request.headers.get("content-type", ""))
    return {
        "request_trace_id": request_trace_id,
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "headers": redact_for_log(
            {
                "content-type": request.headers.get("content-type"),
                "x-api-key": request.headers.get("x-api-key"),
                "authorization": request.headers.get("authorization"),
            }
        ),
        "body": _limit_body(parsed_body, max_body_chars=max_body_chars),
    }


def _response_info(
    request: Request,
    response: Response,
    body: bytes,
    elapsed_ms: int,
    request_trace_id: str,
    *,
    max_body_chars: int,
) -> dict[str, Any]:
    parsed_body = _parse_body(body, response.headers.get("content-type", ""))
    return {
        "request_trace_id": request_trace_id,
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "body": _limit_body(parsed_body, max_body_chars=max_body_chars),
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
