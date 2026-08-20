"""Typed runtime errors used by durable agent execution."""


class RunCancelledError(RuntimeError):
    """Raised at a cooperative runtime boundary after cancellation is requested."""


class ExternalSideEffectOutcomeUnknownError(RuntimeError):
    """Raised when a side-effecting external call may have completed."""


class StageStateError(RuntimeError):
    """Raised when persisted stage state is incompatible with current code/input."""


class StageAttemptsExhaustedError(RuntimeError):
    """Raised when a failed stage has no attempts remaining."""


class StaleStageExecutionError(RuntimeError):
    """Raised when a superseded worker tries to commit a stage result."""
