"""Run Pi's model/tool loop in a local Node process while the platform owns the task."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import re
import time
from copy import deepcopy
from typing import Any, get_args
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError
from pydantic_core import ErrorType

from domain import LogEvent, ModelCallRecord, utc_now
from framework.runtime.context import AgentContext
from framework.runtime.pi.tools import PiTools
from framework.runtime.pi.transport import PiProcess
from framework.runtime.utils import normalize_openai_base_url
from framework.skill.catalog import compose_instructions

_VALIDATION_ERROR_TYPES = frozenset(get_args(ErrorType))
logger = logging.getLogger(__name__)


class PiAgent:
    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        # Task-level recovery only: completed results are reused; interrupted attempts restart the loop.
        async def execute(_stage: Any) -> dict[str, Any]:
            return await self._run_once(context, input_data)

        return await context.run_stage("pi-agent", input_data, execute, definition_version="1")

    async def _run_once(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        runtime = deepcopy(self.runtime)
        model = _model_config(context, runtime)
        max_turns = _positive_int(runtime.get("max_turns", 10), "max_turns")
        max_tool_calls = _positive_int(runtime.get("max_tool_calls", 100), "max_tool_calls")
        max_parallel_tools = _positive_int(runtime.get("max_parallel_tools", 8), "max_parallel_tools")
        if max_parallel_tools > 64:
            raise ValueError("Pi max_parallel_tools must not exceed 64")
        catalog_max_chars = _positive_int(runtime.get("skill_catalog_max_chars", 8_000), "skill_catalog_max_chars")
        execution = runtime.get("tool_execution", "parallel")
        if not isinstance(execution, str) or execution not in {"parallel", "sequential"}:
            raise ValueError("Pi tool_execution must be parallel or sequential")
        thinking = runtime.get("thinking_level", "off")
        if not isinstance(thinking, str) or thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid Pi thinking_level")
        allow_scripts = runtime.get("allow_skill_scripts", False)
        if type(allow_scripts) is not bool:
            raise ValueError("Pi allow_skill_scripts must be a boolean")
        tools = PiTools(context, allow_scripts=allow_scripts)
        prompt_name = runtime.get("prompt_name")
        prompt_spec = (
            context.state_client.get_prompt_spec(prompt_name, runtime.get("prompt_version")) if prompt_name else None
        )
        base = str(
            prompt_spec.content
            if prompt_spec
            else (
                runtime.get("instructions")
                or runtime.get("prompt")
                or context.agent.description
                or "Complete the task using the available tools."
            )
        )
        output_adapter = _output_adapter(runtime.get("output_type"))
        output_fields: set[str] = set()
        if output_adapter is not None:
            schema = output_adapter.json_schema()
            output_fields = _schema_fields(schema)
            base += "\nReturn a final JSON answer matching this schema:\n" + json.dumps(schema)
        prompt_version = prompt_spec.version if prompt_spec else runtime.get("prompt_version") or context.agent.version
        prompt_input = {
            "run_id": context.run_id,
            "route_tag": context.route_tag,
            "input": input_data,
            "metadata": context.metadata,
        }
        template = runtime.get("input_template")
        rendered = (
            template.format(
                run_id=context.run_id,
                route_tag=context.route_tag,
                input=_input_json(input_data),
                metadata=_input_json(context.metadata),
            )
            if template
            else _input_json(prompt_input)
        )
        active: dict[int, ModelCallRecord] = {}
        unfinished: set[int] = set()
        tool_identities: set[tuple[int, str]] = set()
        running_tools: set[tuple[int, str]] = set()
        tool_slots = asyncio.Semaphore(max_parallel_tools)
        process = PiProcess()

        def compose():
            return compose_instructions(
                base_instructions=base,
                session=context.skills,
                include_catalog=True,
                catalog_max_chars=catalog_max_chars,
            )

        def log(event_type: str, **data: Any) -> None:
            context.state_client.add_log(
                LogEvent(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    component="pi_runtime",
                    event_type=event_type,
                    message=event_type.replace("_", " "),
                    data=data,
                )
            )

        async def handle(record: dict[str, Any]) -> dict[str, Any]:
            kind = record.get("kind")
            turn = record.get("turn")
            if type(turn) is not int or turn < 1 or turn > max_turns:
                raise ValueError("Invalid Pi turn identity")
            if kind == "tool_call":
                name, call_id = record.get("name"), record.get("tool_call_id")
                if (
                    turn != len(active)
                    or turn in unfinished
                    or turn not in active
                    or active[turn].status != "succeeded"
                    or active[turn].metadata.get("stop_reason") != "toolUse"
                    or not isinstance(name, str)
                    or name not in tools.platform_names | tools.skill_names
                    or not isinstance(call_id, str)
                    or not call_id
                ):
                    raise ValueError("Invalid Pi tool request identity")
                key = (turn, call_id)
                if key in tool_identities or len(tool_identities) >= max_tool_calls:
                    raise ValueError("Duplicate or over-budget Pi tool request")
                tool_identities.add(key)
                # Provider-generated IDs are untrusted strings, not safe log fields.
                identity = {
                    "tool": name,
                    "turn": turn,
                    "tool_call_id_hash": hashlib.sha256(call_id.encode()).hexdigest()[:16],
                }
                running_tools.add(key)
                try:
                    async with tool_slots:
                        started = time.monotonic()
                        log("pi_tool_started", **identity)
                        try:
                            result = await tools.call(name, record.get("arguments"))
                        except (Exception, asyncio.CancelledError) as exc:
                            try:
                                log(
                                    "pi_tool_failed",
                                    **identity,
                                    error_type=type(exc).__name__,
                                    latency_ms=round((time.monotonic() - started) * 1000),
                                )
                            except Exception:
                                logger.warning(
                                    "Pi tool failure audit unavailable: run_id=%s tool=%s", context.run_id, name
                                )
                            raise
                        log(
                            "pi_tool_finished",
                            **identity,
                            is_error=result["is_error"],
                            latency_ms=round((time.monotonic() - started) * 1000),
                        )
                        return result
                finally:
                    running_tools.remove(key)
            if kind == "prepare_request":
                if turn != len(active) + 1 or unfinished or running_tools:
                    raise ValueError("Duplicate or out-of-order Pi turn")
                message_count = _metric(record.get("message_count"), "message count")
                composed = compose()
                call = ModelCallRecord(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    agent_name=context.agent.name,
                    agent_version=context.agent.version,
                    provider="openai_compatible",
                    model=model["model"],
                    prompt_version=prompt_version,
                    prompt_hash=hashlib.sha256(composed.fingerprint_content.encode()).hexdigest(),
                    # Metadata only: raw prompts, reasoning and business output are not copied into logs.
                    metadata={
                        "runtime": "pi",
                        "turn": turn,
                        "prompt_name": prompt_name,
                        "message_count": message_count,
                        "skills": composed.provenance,
                        "skill_catalog_hash": composed.catalog_hash,
                    },
                )
                active[turn] = call
                unfinished.add(turn)
                context.state_client.save_model_call(call)
                log("pi_model_started", turn=turn, call_id=call.call_id)
                return {"instructions": composed.instructions}
            if kind == "model_end" and turn in unfinished:
                call = active[turn]
                if call.status != "started":
                    raise ValueError("Duplicate Pi model completion")
                usage = record.get("usage")
                if usage is None:
                    usage = {}
                if not isinstance(usage, dict):
                    raise ValueError("Invalid Pi usage metadata")
                prompt_tokens = _metric(
                    sum(_metric(usage.get(key, 0), "usage") for key in ("input", "cacheRead", "cacheWrite")), "usage"
                )
                completion_tokens = _metric(usage.get("output", 0), "usage")
                total_tokens = _metric(usage.get("totalTokens", 0), "usage") or prompt_tokens + completion_tokens
                _metric(total_tokens, "usage")
                estimated_cost = total_tokens * model["cost_per_1k_tokens"] / 1000
                if total_tokens < prompt_tokens + completion_tokens or not math.isfinite(estimated_cost):
                    raise ValueError("Invalid Pi usage metadata")
                reason = record.get("stop_reason")
                if not isinstance(reason, str) or reason not in {"stop", "toolUse", "length", "error", "aborted"}:
                    raise ValueError("Invalid Pi model stop reason")
                call.prompt_tokens = prompt_tokens
                call.completion_tokens = completion_tokens
                call.total_tokens = total_tokens
                call.estimated_cost = estimated_cost
                call.status = "failed" if reason in {"error", "aborted", "length"} else "succeeded"
                call.error = f"Pi model stopped: {reason}" if call.status == "failed" else None
                call.finish_time = utc_now()
                status = record.get("http_status")
                call.http_status = status if type(status) is int and 100 <= status <= 599 else None
                request_id = record.get("provider_request_id")
                call.metadata["provider_request_id"] = (
                    request_id
                    if (isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:/=\-]{1,256}", request_id))
                    else None
                )
                call.metadata["stop_reason"] = reason
                call.metadata["provider_latency_ms"] = _metric(record.get("latency_ms"), "latency")
                for field in ("http_attempts", "request_bytes", "response_bytes"):
                    call.metadata[field] = _metric(record.get(field, 0), field)
                category = record.get("error_category")
                call.metadata["error_category"] = (
                    category
                    if isinstance(category, str)
                    and category
                    in {
                        "timeout",
                        "rate_limit",
                        "authentication",
                        "provider_unavailable",
                        "invalid_request",
                        "content_filter",
                        "network",
                        "unknown",
                    }
                    else None
                )
                context.state_client.save_model_call(call)
                unfinished.remove(turn)
                log("pi_model_finished", turn=turn, call_id=call.call_id, status=call.status)
                return {}
            raise ValueError("Unexpected Pi capability request")

        log("pi_run_started", model=model["model"], max_turns=max_turns)
        try:
            result = await process.run(
                {
                    "run_id": context.run_id,
                    "model": model,
                    "max_turns": max_turns,
                    "max_tool_calls": max_tool_calls,
                    "max_parallel_tools": max_parallel_tools,
                    "tool_execution": execution,
                    "thinking_level": thinking,
                    "tools": tools.definitions,
                    "instructions": compose().instructions,
                    "prompt": rendered,
                },
                handle,
                context.cancellation_signal,
            )
            if (
                not active
                or unfinished
                or result["turns"] != len(active)
                or active[len(active)].status != "succeeded"
                or active[len(active)].metadata.get("stop_reason") != "stop"
            ):
                raise ValueError("Pi worker result does not match the completed model turns")
            text = result["text"]
            if output_adapter is not None:
                try:
                    value = output_adapter.validate_json(text)
                except ValidationError as exc:
                    # Pydantic errors include the rejected business data by default.
                    log(
                        "pi_output_invalid",
                        error_count=exc.error_count(),
                        errors=[
                            {
                                "field": [
                                    part if isinstance(part, int) or part in output_fields else "<field>"
                                    for part in item["loc"]
                                ],
                                "type": item["type"]
                                if item["type"] in _VALIDATION_ERROR_TYPES
                                else "custom_validation_error",
                            }
                            for item in exc.errors(include_input=False, include_context=False, include_url=False)[:20]
                        ],
                    )
                    raise ValueError("Pi output schema validation failed") from None
                except Exception:
                    # Custom validators can raise exceptions not wrapped by Pydantic.
                    raise ValueError("Pi output validation failed") from None
                try:
                    value = output_adapter.dump_python(value, mode="json")
                except Exception:
                    raise ValueError("Pi output serialization failed") from None
            else:
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = text
                except (ValueError, RecursionError):
                    raise ValueError("Pi output JSON decoding failed") from None
            output = value if isinstance(value, dict) else {"data": value}
            output.setdefault("agent", {"name": context.agent.name, "version": context.agent.version})
            _validate_output_json(output)
            log("pi_run_succeeded", turns=result["turns"], output_field_count=len(output))
            return output
        except (Exception, asyncio.CancelledError) as exc:
            for turn in unfinished:
                call = active[turn]
                call.status = "failed"
                call.error = f"Pi attempt interrupted ({type(exc).__name__})"
                call.finish_time = utc_now()
                # A temporarily unavailable audit store must not mask the original failure or
                # prevent attempts to close other in-flight records.
                try:
                    context.state_client.save_model_call(call)
                except Exception:
                    logger.warning(
                        "Pi model record finalization unavailable: run_id=%s call_id=%s", context.run_id, call.call_id
                    )
            try:
                log("pi_run_failed", error_type=type(exc).__name__)
            except Exception:
                logger.warning("Pi failure audit unavailable: run_id=%s", context.run_id)
            raise


def _model_config(context: AgentContext, runtime: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(getattr(context.model_client, "defaults", None) or {})
    config.update({key: value for key, value in runtime.items() if key != "model_config"})
    overrides = runtime.get("model_config", {})
    if not isinstance(overrides, dict):
        raise ValueError("Pi model_config must be an object")
    config.update(overrides)
    if config.get("provider", "openai_compatible") != "openai_compatible":
        raise ValueError("Pi currently supports the platform openai_compatible provider only")
    base_url = config.get("base_url") or ""
    try:
        url = urlsplit(base_url)
        if (
            not isinstance(base_url, str)
            or url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "\\" in base_url
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in base_url)
        ):
            raise ValueError
        _ = url.port  # Reject malformed/out-of-range ports without copying the configured URL.
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Pi requires a valid HTTP(S) base_url without userinfo, query or fragment") from None
    base_url = normalize_openai_base_url(base_url)
    if not base_url or not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ValueError("Pi requires a model and base_url from Agent or platform configuration")
    if config.get("fallback_models"):
        raise ValueError("Pi model fallback is not implemented yet; set model_config.fallback_models=[]")
    if config.get("tools") or config.get("plugins") or config.get("web_search_options"):
        raise ValueError("Provider-native tools/search are not supported by this Pi adapter yet")
    try:
        timeout = float(config["timeout_seconds"] if config.get("timeout_seconds") is not None else 60)
        temperature = float(config.get("temperature", 0))
        price = float(config.get("cost_per_1k_tokens", 0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Pi model timeout, temperature and cost must be numeric") from None
    if (
        not all(math.isfinite(value) for value in (timeout, temperature, price))
        or not 0 < timeout <= 2_147_483.647
        or not 0 <= temperature <= 2
        or price < 0
    ):
        raise ValueError("Pi model timeout, temperature and cost must be finite and valid")
    retries = config.get("max_retries", 2)
    if type(retries) is not int or retries < 0:
        raise ValueError("Pi max_retries must be a nonnegative integer")
    if config.get("api_key") is not None and not isinstance(config["api_key"], str):
        raise ValueError("Pi api_key must be a string")
    if config.get("response_format") is not None and not isinstance(config["response_format"], dict):
        raise ValueError("Pi response_format must be an object")
    max_tokens = _positive_int(config["max_tokens"] if config.get("max_tokens") is not None else 4096, "max_tokens")
    context_window = _positive_int(
        config["context_window"] if config.get("context_window") is not None else 128000,
        "context_window",
    )
    if max_tokens > context_window:
        raise ValueError("Pi max_tokens must not exceed context_window")
    return {
        "model": config["model"],
        "base_url": base_url,
        "api_key": config.get("api_key"),
        "temperature": temperature,
        "timeout_seconds": timeout,
        "max_retries": retries,
        "max_tokens": max_tokens,
        "context_window": context_window,
        "response_format": config.get("response_format"),
        "cost_per_1k_tokens": price,
    }


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError(f"Pi {field} must be a positive integer within the 32-bit range")
    return value


def _input_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("Pi input must contain finite JSON data") from None


def _metric(value: Any, field: str) -> int:
    """Provider metadata must fit PostgreSQL Integer columns; never coerce arbitrary strings."""
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise ValueError(f"Invalid Pi {field} metadata")
    return value


def _validate_output_json(output: Any) -> None:
    """Fail before Stage/Run persistence: PostgreSQL JSONB rejects NUL and unpaired surrogates."""
    try:
        # This also rejects cycles before walking the value, and verifies valid scalar Unicode.
        json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8")
        pending = [output]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                if "\x00" in value:
                    raise ValueError
            elif isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("Pi output must contain finite JSON data with valid PostgreSQL text") from None


def _output_adapter(target: Any) -> TypeAdapter | None:
    if target is None:
        return None
    if isinstance(target, str):
        module, separator, name = target.partition(":")
        if not separator or not module or not name:
            raise ValueError("output_type must use module:attribute syntax")
        target = getattr(importlib.import_module(module), name)
    return TypeAdapter(target)


def _schema_fields(schema: Any) -> set[str]:
    """Only declared field names are safe to log; extra/dictionary keys can be business data."""
    if isinstance(schema, dict):
        return set(schema.get("properties", {})) | set().union(*(_schema_fields(value) for value in schema.values()))
    if isinstance(schema, list):
        return set().union(*(_schema_fields(value) for value in schema))
    return set()
