"""Shared prompt composition for activated Skills."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from framework.skill.session import EmptySkillSession, SkillSession


@dataclass(frozen=True, slots=True)
class ComposedInstructions:
    instructions: str
    fingerprint_content: str
    provenance: list[dict[str, str]]
    catalog_hash: str


def compose_instructions(
    *,
    base_instructions: str,
    session: SkillSession | EmptySkillSession,
    include_catalog: bool = False,
    catalog_max_chars: int = 8_000,
) -> ComposedInstructions:
    """Append activated Skill instructions in declaration order.

    The catalog remains opt-in because adapters without a Skill tool loop must
    not suggest that a model can activate an ``auto`` Skill by itself.
    """
    sections = [base_instructions] if base_instructions.strip() else []
    active = session.active_instructions()
    if active:
        sections.append("## Active skill instructions\n\n" + "\n\n".join(active))
    catalog = session.catalog_prompt(max_chars=catalog_max_chars) if include_catalog else ""
    if catalog:
        sections.append(catalog.strip())
    instructions = "\n\n".join(sections)
    provenance = session.provenance()
    fingerprint_content = (
        base_instructions
        if not provenance and not catalog
        else json.dumps(
            {"instructions": instructions, "skills": provenance},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    catalog_hash = hashlib.sha256(catalog.encode()).hexdigest()
    return ComposedInstructions(
        instructions=instructions,
        fingerprint_content=fingerprint_content,
        provenance=provenance,
        catalog_hash=catalog_hash,
    )
