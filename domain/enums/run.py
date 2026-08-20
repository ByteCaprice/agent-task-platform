"""RunStatus — the lifecycle states an AgentRun moves through.

Goes from CREATED/QUEUED through RUNNING (with WAITING_TOOL / RETRYING) to a
terminal state: SUCCEEDED, FAILED, TIMEOUT or CANCELED. AGENT_SUCCEEDED and
WAITING_CALLBACK are intermediate states after the agent finishes but before
the callback is delivered.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    RETRYING = "RETRYING"
    AGENT_SUCCEEDED = "AGENT_SUCCEEDED"
    WAITING_CALLBACK = "WAITING_CALLBACK"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"


EXECUTING_RUN_STATUSES = frozenset(
    {
        RunStatus.RUNNING,
        RunStatus.WAITING_TOOL,
        RunStatus.RETRYING,
    }
)

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.TIMEOUT,
        RunStatus.CANCELED,
    }
)
