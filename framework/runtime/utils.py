"""Small shared helpers for runtime adapters: build HTTP auth headers, summarize
values for logging, estimate token cost, and normalize an OpenAI base URL.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def headers_for_http_runtime(runtime: dict[str, Any]) -> dict[str, str]:
    headers = {str(key): str(value) for key, value in runtime.get("headers", {}).items()}
    auth = runtime.get("auth") or {}
    auth_type = auth.get("type")
    if not auth_type:
        return headers
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth['token']}"
        return headers
    if auth_type == "api_key":
        header_name = auth.get("header", "X-API-Key")
        headers[str(header_name)] = str(auth["value"])
        return headers
    if auth_type == "basic":
        raw = f"{auth['username']}:{auth['password']}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
        return headers
    raise RuntimeError(f"Unsupported HTTP Agent auth type {auth_type!r}")


def summarize(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def estimate_cost(total_tokens: int, cost_per_1k_tokens: float) -> float:
    if not cost_per_1k_tokens:
        return 0.0
    return round(total_tokens / 1000 * cost_per_1k_tokens, 8)


def normalize_openai_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"
