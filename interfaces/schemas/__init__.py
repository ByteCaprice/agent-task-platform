"""DTOs — API request/response transfer objects (Pydantic).

Grouped by feature (request + its response share a file).  Distinct from
:mod:`domain` (business objects) and :mod:`domain.enums` (shared enums).
"""

from interfaces.schemas.run_submit import RunSubmitRequest, RunSubmitResponse

__all__ = [
    "RunSubmitRequest",
    "RunSubmitResponse",
]
