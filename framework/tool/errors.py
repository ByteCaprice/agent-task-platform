"""Typed tool-layer exceptions used for run error classification."""

from __future__ import annotations

from framework.runtime.errors import ExternalSideEffectOutcomeUnknownError


class ToolExecutionError(RuntimeError):
    """Raised when a tool exhausts retries without a failure handler."""

    def __init__(self, tool_name: str, cause: Exception | None) -> None:
        self.tool_name = tool_name
        self.cause = cause
        super().__init__(f"Tool {tool_name!r} failed: {cause}")


class SideEffectOutcomeUnknownError(ExternalSideEffectOutcomeUnknownError):
    """A side-effecting tool failed after dispatch and must not be replayed."""

    def __init__(self, tool_name: str, cause: BaseException | None) -> None:
        self.tool_name = tool_name
        self.cause = cause
        super().__init__(f"Tool {tool_name!r} outcome is unknown: {cause}")
