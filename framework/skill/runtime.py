"""Composition root for resolving immutable run-local Skill sessions."""

from __future__ import annotations

from domain import AgentSkillRef, AgentSpec, SkillSnapshot, SkillSpec
from framework.registry import SkillRegistry
from framework.skill.errors import SkillValidationError
from framework.skill.loader import SkillLoader
from framework.skill.session import AuditCallback, SkillSession


class SkillRuntime:
    """Combines registry resolution and trusted artifact validation."""

    def __init__(self, *, registry: SkillRegistry, loader: SkillLoader) -> None:
        self.registry = registry
        self.loader = loader

    async def create_session(
        self,
        *,
        run_id: str,
        agent: AgentSpec,
        snapshots: list[SkillSnapshot],
        audit: AuditCallback | None = None,
        max_active_skills: int = 8,
        max_instruction_chars: int = 20_000,
    ) -> SkillSession:
        assignments = self._resolve_snapshots(agent, snapshots)
        for _, spec in assignments:
            self.loader.verify(spec)
        session = SkillSession(
            run_id=run_id,
            assignments=assignments,
            loader=self.loader,
            audit=audit,
            max_active_skills=max_active_skills,
            max_instruction_chars=max_instruction_chars,
        )
        await session.preload_always()
        return session

    def snapshots_for_agent(self, agent: AgentSpec) -> list[SkillSnapshot]:
        """Resolve and verify artifacts before the run record is persisted."""
        specs = self.registry.resolve_for_agent(agent.skills)
        assignments = list(zip(agent.skills, specs, strict=True))
        for _, spec in assignments:
            self.loader.verify(spec)
        return [
            SkillSnapshot(
                name=spec.name,
                version=spec.version,
                content_hash=spec.content_hash,
                activation=ref.activation,
                artifact_id=spec.source_path,
            )
            for ref, spec in assignments
        ]

    def _resolve_snapshots(
        self,
        agent: AgentSpec,
        snapshots: list[SkillSnapshot],
    ) -> list[tuple[AgentSkillRef, SkillSpec]]:
        if not snapshots:
            if agent.skills:
                raise SkillValidationError(
                    f"Run is missing the required Skill snapshot for {agent.name}@{agent.version}"
                )
            return []
        if len(snapshots) != len({snapshot.name for snapshot in snapshots}):
            raise SkillValidationError("Run Skill snapshot contains duplicate Skill names")
        assignments: list[tuple[AgentSkillRef, SkillSpec]] = []
        for snapshot in snapshots:
            spec = self.registry.get_optional(snapshot.name, snapshot.version)
            if spec is None:
                raise SkillValidationError(f"Pinned Skill {snapshot.name}@{snapshot.version} is no longer registered")
            if spec.source_path != snapshot.artifact_id or spec.content_hash != snapshot.content_hash:
                raise SkillValidationError(
                    f"Pinned Skill {snapshot.name}@{snapshot.version} no longer matches its run snapshot"
                )
            self.loader.verify(spec)
            assignments.append(
                (
                    AgentSkillRef(
                        name=snapshot.name,
                        version=snapshot.version,
                        activation=snapshot.activation,
                    ),
                    spec,
                )
            )
        return assignments
