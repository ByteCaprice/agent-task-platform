"""LLM gateway: ``ModelGateway`` runs OpenAI-compatible chat completions with
retry, latency tracking, cost estimation, and model fallback, persisting every
call as a ``ModelCallRecord`` in the store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from domain import LogEvent, ModelCallRecord, utc_now
from framework.runtime.utils import normalize_openai_base_url
from infra.store import RunStore

logger = logging.getLogger("agent_task_platform.model_gateway")

# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# HTTP status codes that should trigger a retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Exception type names that should trigger a retry
RETRYABLE_EXCEPTIONS = {
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
    "PoolTimeout",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
}

# OpenAI-compatible vendors expose a few non-standard request knobs. Keep this
# explicit so agent runtime cannot accidentally pass arbitrary transport fields.
EXTRA_PAYLOAD_KEYS = {"plugins", "web_search_options"}


@dataclass
class RetryPolicy:
    """Configuration for model call retries."""

    max_retries: int = 2
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    retryable_status_codes: set[int] = field(default_factory=lambda: set(RETRYABLE_STATUS_CODES))

    def delay_for(self, attempt: int) -> float:
        """Backoff delay for the given attempt number (0-indexed)."""
        delay = self.initial_backoff_seconds * (self.backoff_multiplier**attempt)
        return min(delay, self.max_backoff_seconds)

    def should_retry(self, *, status_code: int | None = None, exception: Exception | None = None) -> bool:
        """Return True if the error is retryable."""
        if status_code is not None and status_code in self.retryable_status_codes:
            return True
        if exception is not None:
            exc_type_name = type(exception).__name__
            if exc_type_name in RETRYABLE_EXCEPTIONS:
                return True
            # httpx.TimeoutException and subclasses
            if isinstance(exception, (httpx.TimeoutException, httpx.TransportError)):
                return True
            # httpx.HTTPStatusError — check status code
            if isinstance(exception, httpx.HTTPStatusError):
                if exception.response.status_code in self.retryable_status_codes:
                    return True
        return False


class ModelGateway:
    """Production model gateway with retry, latency tracking, and fallback.

    Configuration via ``defaults`` dict (from settings) or per-call
    ``runtime`` overrides.  Supported defaults keys:
    - provider, base_url, api_key, model, timeout_seconds, temperature
    - cost_per_1k_tokens, response_format
    - max_retries, initial_backoff_seconds, max_backoff_seconds
    - fallback_models (list of model names)
    """

    def __init__(
        self,
        *,
        store: RunStore,
        defaults: dict[str, Any] | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.store = store
        self.defaults = defaults or {}
        self.http_client = http_client
        self.timeout_seconds = float(self.defaults.get("timeout_seconds") or 60)
        self.retry_policy = RetryPolicy(
            max_retries=int(self.defaults.get("max_retries", 2)),
            initial_backoff_seconds=float(self.defaults.get("initial_backoff_seconds", 1.0)),
            max_backoff_seconds=float(self.defaults.get("max_backoff_seconds", 30.0)),
        )
        self.fallback_models: list[str] = list(self.defaults.get("fallback_models", []))

    async def complete(
        self,
        *,
        run_id: str,
        trace_id: str,
        agent_name: str,
        agent_version: str,
        input_data: dict[str, Any],
        metadata: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        provider = runtime.get("provider") or self.defaults.get("provider") or "openai_compatible"
        if provider != "openai_compatible":
            raise ValueError(f"Unsupported model provider {provider!r}")
        prompt_name = runtime.get("prompt_name")
        instructions = _instructions(runtime)
        prompt_version = runtime.get("prompt_version") or agent_version
        prompt_fingerprint_content = runtime.get("prompt_fingerprint_content")
        prompt_hash = hashlib.sha256(
            str(prompt_fingerprint_content if prompt_fingerprint_content is not None else instructions).encode()
        ).hexdigest()
        model = runtime.get("model") or self.defaults.get("model")
        fallback_models = list(runtime.get("fallback_models") or self.fallback_models)
        models_to_try = [model] + [m for m in fallback_models if m != model]
        messages = _messages_for_request(
            input_data=input_data,
            metadata=metadata,
            run_id=run_id,
            instructions=instructions,
        )
        record = ModelCallRecord(
            run_id=run_id,
            trace_id=trace_id,
            agent_name=agent_name,
            agent_version=agent_version,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            input_summary=_summarize({"messages": messages}),
            metadata={"runtime": "model_gateway", "prompt_name": prompt_name},
        )
        self.store.model_calls.save(record)
        self.store.logs.add(
            LogEvent(
                run_id=run_id,
                trace_id=trace_id,
                component="model_gateway",
                event_type="model_call_started",
                message="Model Gateway call started",
                data={
                    "call_id": record.call_id,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                },
            )
        )

        last_exc: Exception | None = None
        for model_idx, current_model in enumerate(models_to_try):
            is_fallback = model_idx > 0
            if is_fallback:
                self.store.logs.add(
                    LogEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        component="model_gateway",
                        event_type="model_fallback",
                        message=f"Falling back to model {current_model!r}",
                        data={
                            "call_id": record.call_id,
                            "fallback_model": current_model,
                            "previous_model": models_to_try[model_idx - 1],
                        },
                    )
                )
                record.model = current_model
            record.error = None
            try:
                response_body, http_status, latency_ms, provider_request_id = await self._call_with_retry(
                    runtime,
                    model=current_model,
                    messages=messages,
                )
                content = _content_from_response(response_body)
                annotations = _annotations_from_response(response_body)
                usage = _usage_from_response(response_body)
                output = _parse_output(content)
                if annotations:
                    output.setdefault("annotations", annotations)
                record.status = "succeeded"
                record.prompt_tokens = usage["prompt_tokens"]
                record.completion_tokens = usage["completion_tokens"]
                record.total_tokens = usage["total_tokens"]
                record.estimated_cost = _estimate_cost(
                    record.total_tokens,
                    float(runtime.get("cost_per_1k_tokens") or self.defaults.get("cost_per_1k_tokens") or 0.0),
                )
                record.output_summary = _summarize(output)
                record.finish_time = utc_now()
                record.http_status = http_status
                record.metadata["provider_request_id"] = provider_request_id
                record.metadata["latency_ms"] = latency_ms
                self.store.model_calls.save(record)
                self.store.logs.add(
                    LogEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        component="model_gateway",
                        event_type="model_call_succeeded",
                        message="Model Gateway call succeeded",
                        data={
                            "call_id": record.call_id,
                            "usage": usage,
                            "estimated_cost": record.estimated_cost,
                            "http_status": http_status,
                            "latency_ms": latency_ms,
                            "model": current_model,
                        },
                    )
                )
                output.setdefault("agent", {"name": agent_name, "version": agent_version})
                output.setdefault("model_call_id", record.call_id)
                return output
            except Exception as exc:
                last_exc = exc
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                record.http_status = _extract_http_status(exc)
                if not record.finish_time:
                    record.finish_time = utc_now()
                self.store.model_calls.save(record)
                self.store.logs.add(
                    LogEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        component="model_gateway",
                        event_type="model_call_failed",
                        message=f"Model Gateway call failed on model {current_model!r}",
                        data={
                            "call_id": record.call_id,
                            "error": record.error,
                            "http_status": record.http_status,
                            "model": current_model,
                            "is_fallback": is_fallback,
                        },
                    )
                )
                # If non-retryable (4xx), don't try fallback models
                if not self.retry_policy.should_retry(exception=exc):
                    raise
                # If this was the last fallback model, raise
                if model_idx >= len(models_to_try) - 1:
                    raise

        raise last_exc  # type: ignore[misc]

    async def _call_with_retry(
        self,
        runtime: dict[str, Any],
        *,
        model: str,
        messages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, int, str | None]:
        """Execute model call with retry policy. Returns (response_body, http_status, latency_ms, provider_request_id)."""
        last_exc: Exception | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response, latency_ms = await self._do_request(runtime, model=model, messages=messages)
                http_status = response.status_code
                if response.status_code in self.retry_policy.retryable_status_codes:
                    body_text = response.text[:500] if hasattr(response, "text") else ""
                    if attempt < self.retry_policy.max_retries:
                        delay = self.retry_policy.delay_for(attempt)
                        logger.warning(
                            "Model call got HTTP %d (attempt %d/%d), retrying in %.1fs: %s",
                            http_status,
                            attempt + 1,
                            self.retry_policy.max_retries + 1,
                            delay,
                            body_text,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Retries exhausted — let raise_for_status throw
                response.raise_for_status()
                body = response.json()
                provider_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                return body, http_status, latency_ms, provider_request_id
            except Exception as exc:
                last_exc = exc
                if attempt < self.retry_policy.max_retries and self.retry_policy.should_retry(exception=exc):
                    delay = self.retry_policy.delay_for(attempt)
                    logger.warning(
                        "Model call failed (attempt %d/%d), retrying in %.1fs: %s: %s",
                        attempt + 1,
                        self.retry_policy.max_retries + 1,
                        delay,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_exc  # type: ignore[misc]

    async def _do_request(
        self,
        runtime: dict[str, Any],
        *,
        model: str,
        messages: list[dict[str, Any]],
    ) -> tuple[Any, int]:
        """Execute a single HTTP request. Returns (response, latency_ms)."""
        base_url = normalize_openai_base_url(runtime.get("base_url") or self.defaults.get("base_url") or "")
        if not base_url:
            raise ValueError("model base_url is required")
        api_key = runtime.get("api_key") or self.defaults.get("api_key")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": runtime.get("temperature", self.defaults.get("temperature", 0)),
        }
        if runtime.get("response_format") or self.defaults.get("response_format"):
            payload["response_format"] = runtime.get("response_format") or self.defaults.get("response_format")
        for key in EXTRA_PAYLOAD_KEYS:
            if runtime.get(key) is not None:
                payload[key] = runtime[key]
            elif self.defaults.get(key) is not None:
                payload[key] = self.defaults[key]
        start = time.monotonic()
        response = await self._post_json(f"{base_url}/chat/completions", headers=headers, payload=payload)
        latency_ms = int((time.monotonic() - start) * 1000)
        return response, latency_ms

    async def _post_json(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        if self.http_client is not None:
            return await self.http_client.post(url, headers=headers, json=payload)
        client = httpx.AsyncClient(timeout=self.timeout_seconds)
        if hasattr(client, "__aenter__"):
            async with client as active_client:
                return await active_client.post(url, headers=headers, json=payload)
        try:
            return await client.post(url, headers=headers, json=payload)
        finally:
            close = getattr(client, "aclose", None)
            if close:
                await close()


def _extract_http_status(exc: Exception) -> int | None:
    """Extract HTTP status code from an httpx exception."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _instructions(runtime: dict[str, Any]) -> str:
    return runtime.get("instructions") or "Return a JSON object for the run input."


def _messages_for_request(
    *,
    input_data: dict[str, Any],
    metadata: dict[str, Any],
    run_id: str,
    instructions: str,
) -> list[dict[str, Any]]:
    raw_messages = input_data.get("messages")
    if isinstance(raw_messages, list):
        messages = [message for message in raw_messages if isinstance(message, dict)]
        if messages:
            if instructions and not any(message.get("role") == "system" for message in messages):
                return [{"role": "system", "content": instructions}, *messages]
            return messages
    return [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": json.dumps(
                {"input": input_data, "metadata": metadata, "run_id": run_id},
                ensure_ascii=False,
            ),
        },
    ]


def _content_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _annotations_from_response(response: dict[str, Any]) -> list[Any]:
    choices = response.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    annotations = message.get("annotations") or []
    return annotations if isinstance(annotations, list) else []


def _usage_from_response(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _parse_output(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"data": content}
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}


def _estimate_cost(total_tokens: int, cost_per_1k_tokens: float) -> float:
    if not cost_per_1k_tokens:
        return 0.0
    return round(total_tokens / 1000 * cost_per_1k_tokens, 8)


def _summarize(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
