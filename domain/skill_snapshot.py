"""Immutable Skill artifact identity pinned to one Agent run."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SkillSnapshot(BaseModel):
    """The deployed artifact identity resolved before a run begins.

    `artifact_id` acts as the source artifact locator (e.g., local directory name
    or `db://<name>@<version>`), while `content_hash` guarantees strict content immutability.
    """

    name: str
    version: str
    content_hash: str
    activation: Literal["auto", "explicit", "always"]
    artifact_id: str
