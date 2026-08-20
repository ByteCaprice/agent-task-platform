"""Task Error Handlers — structured error recovery pattern (inspired by OpenAI Agents SDK).

Defines a typed dict of error-kind → handler mappings. Each handler receives the
error plus a snapshot of run state, and can return either a graceful fallback
result or ``None`` to re-raise the original error.

Usage::

    handlers = RunErrorHandlers()
    handlers.set("timeout", my_timeout_handler)
    handlers.set("tool_failure", my_tool_handler)

    result = await handlers.handle("timeout", input)
    if result is not None:
        return result  # graceful degradation
    raise  # no handler, or handler returned None
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunErrorInput:
    """Snapshot of run state when an error occurred."""

    error: Exception
    """The original exception."""

    run_id: str
    trace_id: str
    route_tag: str
    caller: str = ""
    agent_name: str = ""
    agent_version: str = ""
    attempts: int = 0
    input_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    partial_output: dict[str, Any] | None = None
    current_stage: str = ""


@dataclass
class RunErrorResult:
    """Result returned by an error handler to gracefully end the run."""

    final_output: dict[str, Any] = field(default_factory=dict)
    """The degraded/fallback output to store as the run result."""

    status: str = "degraded"
    """Task status. Use 'degraded' for graceful fallback, 'failed' for explicit failure."""

    include_error_in_output: bool = True
    """If True, the original error details are included in final_output under '_error'."""


RunErrorHandlerFn = Callable[[RunErrorInput], Awaitable[RunErrorResult | None]]
"""Receives error + snapshot, returns a fallback result or None to re-raise."""


class RunErrorHandlers:
    """Registry of error-kind → handler mappings for run execution.

    Supported keys: ``timeout``, ``tool_failure``, ``validation_error``, ``agent_error``.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, RunErrorHandlerFn] = {}

    def set(self, kind: str, handler: RunErrorHandlerFn) -> None:
        self._handlers[kind] = handler

    def remove(self, kind: str) -> None:
        self._handlers.pop(kind, None)

    @property
    def registered_kinds(self) -> list[str]:
        return list(self._handlers.keys())

    async def handle(self, kind: str, input_data: RunErrorInput) -> RunErrorResult | None:
        """Invoke the handler for the given error kind.

        Returns the handler's result, or None if no handler is registered
        or the handler chooses to re-raise.
        """
        handler = self._handlers.get(kind)
        if handler is None:
            return None
        return await handler(input_data)
