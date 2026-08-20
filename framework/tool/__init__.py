"""Tool runtime package: re-exports the ``ToolGateway``, the ``function_tool``
decorator, guardrails, hooks, MCP support, health checks, and error handlers.
"""

from .error_handlers import RunErrorHandlers, RunErrorInput, RunErrorResult
from .function_tool import FuncSchema, ToolError, function_schema, function_tool
from .guardrails import (
    GuardrailBehavior,
    GuardrailFunctionOutput,
    GuardrailResult,
    GuardrailTripwireTriggered,
    InputGuardrail,
    OutputGuardrail,
    ToolGuardrailOutput,
    ToolGuardrailTripwireTriggered,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    run_input_guardrails,
    run_output_guardrails,
    run_tool_input_guardrails,
    run_tool_output_guardrails,
)
from .health import check_agent_health, check_tool_health
from .hooks import CompositeHooks, HookContext, LoggingHooks, RunHooks
from .mcp import MCPError, MCPServerManager
from .stage_context import StageExecutionContext
from .tools import ToolGateway
