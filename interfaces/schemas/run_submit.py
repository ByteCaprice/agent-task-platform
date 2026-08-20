"""DTOs for the native `POST /v1/runs` endpoint: run submit request/response."""

from __future__ import annotations

from pydantic import BaseModel

from domain.enums.run import RunStatus
from domain.run_submission import RunSubmission


class RunSubmitRequest(RunSubmission):
    """HTTP request body for ``POST /v1/runs``.

    Wire-format view of the :class:`~domain.run_submission.RunSubmission`
    use-case input; adds no fields today but marks the API boundary and is
    where API-only concerns (aliases, examples) would live.
    """


class RunSubmitResponse(BaseModel):
    run_id: str
    status: RunStatus
    trace_id: str
    conversation_id: str | None = None
