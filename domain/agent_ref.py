"""AgentRef — a lightweight pointer to a specific agent by name and version.

Used to record which agent (and which version of it) handled a run.
"""

from __future__ import annotations

from pydantic import BaseModel


class AgentRef(BaseModel):
    name: str
    version: str
