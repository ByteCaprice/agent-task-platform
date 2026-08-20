"""Small logging helpers shared by HTTP, tool, and callback observability."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "password",
    "passwd",
    "secret",
    "signing_secret",
    "signature",
    "token",
    "access_token",
    "ossaccesskeyid",
}

REDACTED = "***REDACTED***"
REQUEST_TRACE_HEADER = "X-Request-Trace-Id"
REQUEST_TRACE_ID_EMPTY = "-"

_REQUEST_TRACE_ID: ContextVar[str] = ContextVar("agent_task_platform_request_trace_id", default=REQUEST_TRACE_ID_EMPTY)
_LOG_RECORD_FACTORY_INSTALLED = False


def safe_url(value: str) -> str:
    """Return a URL suitable for logs: path is kept, query/fragment removed."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def new_request_trace_id() -> str:
    return f"REQ-{uuid4().hex}"


def get_request_trace_id() -> str:
    return _REQUEST_TRACE_ID.get()


def set_request_trace_id(request_trace_id: str) -> Token[str]:
    return _REQUEST_TRACE_ID.set(request_trace_id or REQUEST_TRACE_ID_EMPTY)


def reset_request_trace_id(token: Token[str]) -> None:
    _REQUEST_TRACE_ID.reset(token)


def install_request_trace_log_record_factory() -> None:
    global _LOG_RECORD_FACTORY_INSTALLED
    if _LOG_RECORD_FACTORY_INSTALLED:
        return

    original_factory = logging.getLogRecordFactory()

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original_factory(*args, **kwargs)
        record.req_id = get_request_trace_id()
        return record

    logging.setLogRecordFactory(record_factory)
    _LOG_RECORD_FACTORY_INSTALLED = True


def redact_for_log(value: Any, *, max_string: int = 512) -> Any:
    """Redact secrets while preserving enough structure for debugging."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace("_", "").replace("-", "")
            if key_text.lower() in SENSITIVE_KEYS or normalized in SENSITIVE_KEYS:
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_for_log(item, max_string=max_string)
        return redacted
    if isinstance(value, list):
        return [redact_for_log(item, max_string=max_string) for item in value]
    if isinstance(value, tuple):
        return [redact_for_log(item, max_string=max_string) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        if "://" in value:
            value = safe_url(value)
        if len(value) > max_string:
            return f"{value[:max_string]}...<truncated>"
        return value
    return value


def log_json(value: Any, *, max_chars: int = 4096) -> str:
    """Serialize a redacted object into one compact log field."""
    text = json.dumps(redact_for_log(value), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > max_chars:
        return f"{text[:max_chars]}...<truncated>"
    return text
