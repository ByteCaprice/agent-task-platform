"""ErrorType — classification of why a run failed.

Also decides retryability: ``is_retryable()`` returns False only for the
non-retryable kinds (validation and route-not-found), which won't be retried.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorType(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    AGENT_NOT_AVAILABLE = "AGENT_NOT_AVAILABLE"
    AGENT_EXECUTION_ERROR = "AGENT_EXECUTION_ERROR"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_RATE_LIMITED = "TOOL_RATE_LIMITED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    MODEL_ERROR = "MODEL_ERROR"
    RUN_TIMEOUT = "RUN_TIMEOUT"
    CALLBACK_ERROR = "CALLBACK_ERROR"
    SIDE_EFFECT_OUTCOME_UNKNOWN = "SIDE_EFFECT_OUTCOME_UNKNOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    def is_retryable(self) -> bool:
        return self not in _NON_RETRYABLE_ERRORS


_NON_RETRYABLE_ERRORS = frozenset(
    {
        ErrorType.VALIDATION_ERROR,
        ErrorType.ROUTE_NOT_FOUND,
        ErrorType.SIDE_EFFECT_OUTCOME_UNKNOWN,
    }
)
