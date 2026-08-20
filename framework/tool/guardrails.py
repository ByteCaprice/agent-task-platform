"""Task and Tool Guardrails — input/output safety checks (inspired by OpenAI Agents SDK).

Two layers:
- **Task-level**: InputGuardrail / OutputGuardrail — check full run input/output.
- **Tool-level**: ToolInputGuardrail / ToolOutputGuardrail — check individual tool calls.

All guardrails use a three-behavior model: allow, reject_content, raise_exception.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .hooks import HookContext


class GuardrailBehavior(StrEnum):
    ALLOW = "allow"
    REJECT_CONTENT = "reject_content"
    RAISE_EXCEPTION = "raise_exception"


# ── Task-level guardrails ────────────────────────────────────────────────────


@dataclass
class GuardrailFunctionOutput:
    output_info: Any = None
    tripwire_triggered: bool = False


class GuardrailTripwireTriggered(Exception):
    def __init__(self, guardrail_name: str, output: GuardrailFunctionOutput) -> None:
        self.guardrail_name = guardrail_name
        self.guardrail_output = output
        super().__init__(f"Guardrail '{guardrail_name}' triggered: {output.output_info}")


GuardrailFunction = Callable[
    [HookContext, Any],
    Awaitable[GuardrailFunctionOutput],
]


@dataclass
class GuardrailResult:
    guardrail_name: str
    output: GuardrailFunctionOutput


class InputGuardrail:
    def __init__(self, guardrail_function: GuardrailFunction, name: str | None = None) -> None:
        self.guardrail_function = guardrail_function
        self.name = name or getattr(guardrail_function, "__name__", "unknown")


class OutputGuardrail:
    def __init__(self, guardrail_function: GuardrailFunction, name: str | None = None) -> None:
        self.guardrail_function = guardrail_function
        self.name = name or getattr(guardrail_function, "__name__", "unknown")


async def run_input_guardrails(
    guardrails: list[InputGuardrail],
    context: HookContext,
    input_data: dict[str, Any],
) -> list[GuardrailResult]:
    results: list[GuardrailResult] = []
    for g in guardrails:
        output = await g.guardrail_function(context, input_data)
        results.append(GuardrailResult(guardrail_name=g.name, output=output))
        if output.tripwire_triggered:
            raise GuardrailTripwireTriggered(g.name, output)
    return results


async def run_output_guardrails(
    guardrails: list[OutputGuardrail],
    context: HookContext,
    output: dict[str, Any],
) -> list[GuardrailResult]:
    results: list[GuardrailResult] = []
    for g in guardrails:
        go = await g.guardrail_function(context, output)
        results.append(GuardrailResult(guardrail_name=g.name, output=go))
        if go.tripwire_triggered:
            raise GuardrailTripwireTriggered(g.name, go)
    return results


# ── Tool-level guardrails (NEW) ──────────────────────────────────────────────

ToolGuardrailFn = Callable[
    [HookContext, dict[str, Any]],
    Awaitable["ToolGuardrailOutput"],
]


@dataclass
class ToolGuardrailOutput:
    behavior: GuardrailBehavior = GuardrailBehavior.ALLOW
    message: str | None = None
    output_info: Any = None


class ToolGuardrailTripwireTriggered(Exception):
    def __init__(self, guardrail_name: str, output: ToolGuardrailOutput) -> None:
        self.guardrail_name = guardrail_name
        self.guardrail_output = output
        super().__init__(f"Tool guardrail '{guardrail_name}' halted: {output.message}")


class ToolInputGuardrail:
    def __init__(self, guardrail_function: ToolGuardrailFn, name: str | None = None) -> None:
        self.guardrail_function = guardrail_function
        self.name = name or getattr(guardrail_function, "__name__", "unknown")


class ToolOutputGuardrail:
    def __init__(self, guardrail_function: ToolGuardrailFn, name: str | None = None) -> None:
        self.guardrail_function = guardrail_function
        self.name = name or getattr(guardrail_function, "__name__", "unknown")


async def run_tool_input_guardrails(
    guardrails: list[ToolInputGuardrail],
    context: HookContext,
    input_data: dict[str, Any],
) -> list[ToolGuardrailOutput]:
    results: list[ToolGuardrailOutput] = []
    for g in guardrails:
        output = await g.guardrail_function(context, input_data)
        results.append(output)
        if output.behavior == GuardrailBehavior.RAISE_EXCEPTION:
            raise ToolGuardrailTripwireTriggered(g.name, output)
    return results


async def run_tool_output_guardrails(
    guardrails: list[ToolOutputGuardrail],
    context: HookContext,
    output: dict[str, Any],
) -> list[ToolGuardrailOutput]:
    results: list[ToolGuardrailOutput] = []
    for g in guardrails:
        go = await g.guardrail_function(context, output)
        results.append(go)
        if go.behavior == GuardrailBehavior.RAISE_EXCEPTION:
            raise ToolGuardrailTripwireTriggered(g.name, go)
    return results
