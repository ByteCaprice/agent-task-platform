"""Immutable, run-scoped Skill activation state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from domain import AgentSkillRef, SkillSpec
from framework.skill.errors import (
    SkillActivationDeniedError,
    SkillNotAssignedError,
    SkillNotFoundError,
    SkillResourceError,
    SkillScriptError,
)
from framework.skill.loader import ResourceHandle, SkillLoader

AuditCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    version: str
    description: str
    activation: str


@dataclass(frozen=True, slots=True)
class SkillActivation:
    name: str
    version: str
    content_hash: str
    instructions: str
    activation: str


class SkillSession:
    """Keeps artifact snapshots and activation state private to a single Run."""

    def __init__(
        self,
        *,
        run_id: str,
        assignments: list[tuple[AgentSkillRef, SkillSpec]],
        loader: SkillLoader,
        audit: AuditCallback | None = None,
        max_active_skills: int = 8,
        max_instruction_chars: int = 20_000,
    ) -> None:
        self.run_id = run_id
        self._loader = loader
        self._audit = audit or (lambda _event_type, _data: None)
        self._max_active_skills = max_active_skills
        self._max_instruction_chars = max_instruction_chars
        self._assignments: dict[str, tuple[AgentSkillRef, SkillSpec]] = {}
        self._order: list[str] = []
        self._last_truncated_audit: tuple[int, tuple[str, ...], int] | None = None
        for ref, spec in assignments:
            if ref.name in self._assignments:
                raise SkillNotAssignedError(f"Skill {ref.name!r} is assigned more than once")
            if ref.name != spec.name or ref.version not in {None, spec.version}:
                raise SkillNotAssignedError(f"Skill assignment does not match resolved artifact {ref.name!r}")
            self._assignments[ref.name] = (ref.model_copy(deep=True), spec.model_copy(deep=True))
            self._order.append(ref.name)
        self._active: dict[str, SkillActivation] = {}

    async def preload_always(self) -> None:
        for name in self._order:
            ref, _ = self._assignments[name]
            if ref.activation == "always":
                await self.activate(name, reason="activation policy is always", explicit=True)

    def catalog(self) -> list[SkillMetadata]:
        return [
            SkillMetadata(name=name, version=spec.version, description=spec.description, activation=ref.activation)
            for name in self._order
            for ref, spec in [self._assignments[name]]
        ]

    def catalog_prompt(self, *, max_chars: int = 8_000) -> str:
        if not self.catalog():
            return ""
        header = (
            "Available skills are listed below. Load a skill only when the task matches its description.\n"
            "Use skill_load with the exact name before following that skill's workflow.\n\n"
        )
        if max_chars <= len(header):
            return ""
        lines: list[str] = []
        omitted: list[str] = []
        remaining = max_chars - len(header)
        for metadata in self.catalog():
            line = f"- {metadata.name}@{metadata.version}: {metadata.description}\n"
            if len(line) <= remaining:
                lines.append(line)
                remaining -= len(line)
                continue
            prefix = f"- {metadata.name}@{metadata.version}: "
            available_description = remaining - len(prefix) - 4
            if available_description > 0:
                lines.append(f"{prefix}{metadata.description[:available_description]}...\n")
                remaining = 0
                omitted.extend(item.name for item in self.catalog()[len(lines) :])
            else:
                omitted.extend(item.name for item in self.catalog()[len(lines) :])
            break
        if omitted:
            omitted_list = sorted(set(omitted))
            audit_key = (len(lines), tuple(omitted_list), max_chars)
            if self._last_truncated_audit != audit_key:
                self._last_truncated_audit = audit_key
                self._audit(
                    "skill_catalog_truncated",
                    {"included": len(lines), "omitted": omitted_list, "budget": max_chars},
                )
        return header + "".join(lines)

    async def activate(self, name: str, *, reason: str, explicit: bool = False) -> SkillActivation:
        assignment = self._assignments.get(name)
        if assignment is None:
            self._audit("skill_activation_denied", {"name": name, "reason": "not_assigned"})
            raise SkillNotAssignedError(f"Skill {name!r} is not assigned to this Agent")
        ref, spec = assignment
        if ref.activation == "explicit" and not explicit:
            self._audit(
                "skill_activation_denied",
                {
                    "name": name,
                    "version": spec.version,
                    "reason": "explicit_required",
                },
            )
            raise SkillActivationDeniedError(f"Skill {name!r} requires explicit activation")
        active = self._active.get(name)
        if active is not None:
            return active
        if len(self._active) >= self._max_active_skills:
            self._audit(
                "skill_activation_denied",
                {
                    "name": name,
                    "version": spec.version,
                    "reason": "activation_limit",
                },
            )
            raise SkillActivationDeniedError("Skill activation limit reached for this Run")
        instructions = self._loader.load_instructions(spec)
        current_size = sum(len(item.instructions) for item in self._active.values())
        if current_size + len(instructions) > self._max_instruction_chars:
            self._audit(
                "skill_activation_denied",
                {
                    "name": name,
                    "version": spec.version,
                    "reason": "instruction_budget",
                },
            )
            raise SkillActivationDeniedError("Skill instruction budget exceeded for this Run")
        activation = SkillActivation(
            name=name,
            version=spec.version,
            content_hash=spec.content_hash,
            instructions=instructions,
            activation="always" if ref.activation == "always" else "explicit" if explicit else "implicit",
        )
        self._active[name] = activation
        self._audit(
            "skill_activated",
            {
                "name": name,
                "version": spec.version,
                "content_hash": spec.content_hash,
                "activation": activation.activation,
                "reason": reason,
                "instruction_chars": len(instructions),
            },
        )
        return activation

    async def read_text_resource(self, name: str, path: str) -> str:
        spec = self._active_spec(name)
        text = self._loader.read_text_resource(spec, path)
        self._audit(
            "skill_resource_read",
            {
                "name": name,
                "version": spec.version,
                "path": path,
                "bytes": len(text.encode()),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            },
        )
        return text

    async def open_binary_resource(self, name: str, path: str) -> ResourceHandle:
        spec = self._active_spec(name)
        handle = self._loader.open_binary_resource(spec, path)
        self._audit(
            "skill_resource_read",
            {
                "name": name,
                "version": spec.version,
                "path": path,
                "bytes": len(handle.content),
                "content_hash": hashlib.sha256(handle.content).hexdigest(),
            },
        )
        return handle

    async def run_script(self, name: str, script: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self._active_spec(name)
        from framework.skill.scripts import SkillScriptRunner

        return await SkillScriptRunner.run(
            spec,
            script,
            arguments,
            loader=self._loader,
            audit=self._audit,
        )

    def active_instructions(self) -> list[str]:
        return [self._active[name].instructions for name in self._order if name in self._active]

    def provenance(self) -> list[dict[str, str]]:
        return [
            {
                "name": self._active[name].name,
                "version": self._active[name].version,
                "content_hash": self._active[name].content_hash,
                "activation": self._active[name].activation,
            }
            for name in self._order
            if name in self._active
        ]

    def resolved_provenance(self) -> list[dict[str, str]]:
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "content_hash": spec.content_hash,
                "activation": ref.activation,
            }
            for name in self._order
            for ref, spec in [self._assignments[name]]
        ]

    def snapshot_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.resolved_provenance(), sort_keys=True).encode()).hexdigest()

    def _active_spec(self, name: str) -> SkillSpec:
        if name not in self._assignments:
            raise SkillNotAssignedError(f"Skill {name!r} is not assigned to this Agent")
        if name not in self._active:
            raise SkillResourceError(f"Skill {name!r} must be activated before its resources can be read")
        return self._assignments[name][1]


class EmptySkillSession:
    """Compatibility session for legacy Agents and direct context construction."""

    async def preload_always(self) -> None:
        return None

    def catalog(self) -> list[SkillMetadata]:
        return []

    def catalog_prompt(self, *, max_chars: int = 8_000) -> str:
        return ""

    async def activate(self, name: str, *, reason: str, explicit: bool = False) -> SkillActivation:
        raise SkillNotFoundError(f"Skill support is not configured for this Run: {name!r}")

    async def read_text_resource(self, name: str, path: str) -> str:
        raise SkillNotFoundError(f"Skill support is not configured for this Run: {name!r}")

    async def open_binary_resource(self, name: str, path: str) -> ResourceHandle:
        raise SkillNotFoundError(f"Skill support is not configured for this Run: {name!r}")

    async def run_script(self, name: str, script: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise SkillScriptError("Skill scripts are not executable")

    def active_instructions(self) -> list[str]:
        return []

    def provenance(self) -> list[dict[str, str]]:
        return []

    def resolved_provenance(self) -> list[dict[str, str]]:
        return []

    def snapshot_hash(self) -> str:
        return hashlib.sha256(b"[]").hexdigest()
