"""RunFile — a single file attached to a run's input.

Holds a reference to a file (by id or url) plus its name, mime type and any
extra metadata that an agent may need when processing the run.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunFile(BaseModel):
    file_id: str | None = None
    url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
