"""RunSubmission — the transport-agnostic request to start a new run.

The domain input that the application layer accepts to create an
AgentRun, independent of any HTTP/wire format.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from domain.callback_config import CallbackConfig
from domain.run_file import RunFile


class RunSubmission(BaseModel):
    """Transport-agnostic input for the run-submission use-case.

    The application layer (``RunService`` / ``RunManager``) depends on this
    domain object, never on an HTTP DTO.  Interface adapters
    (``interfaces/http``) map their wire formats into a ``RunSubmission``.
    """

    route_tag: str
    request_id: str
    external_id: str | None = None
    task_type: str | None = None
    source: str | None = None
    conversation_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    files: list[RunFile] = Field(default_factory=list)
    callback: CallbackConfig | None = None
    priority: int = 5
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    caller: str = "default"
    agent_version: str | None = None
