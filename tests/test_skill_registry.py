from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain import AgentRun, AgentSkillRef, AgentSpec
from framework.registry import RegistryError, SkillRegistry
from framework.runtime.agent_runtime import AgentRuntime
from framework.runtime.context import AgentContext
from framework.skill import (
    SkillActivationDeniedError,
    SkillDisabledError,
    SkillLoader,
    SkillNotAssignedError,
    SkillNotFoundError,
    SkillResourceError,
    SkillScriptError,
)
from framework.skill.runtime import SkillRuntime


def _write_skill(root: Path, name: str, version: str) -> None:
    skill = root / name
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Use {name} for testing.\n"
        "metadata:\n"
        f'  version: "{version}"\n'
        "---\n"
        "\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text(f"{name} guide\n", encoding="utf-8")


def _runtime(root: Path) -> tuple[SkillRuntime, SkillRegistry]:
    _write_skill(root, "auto-skill", "1.0.0")
    _write_skill(root, "always-skill", "2.0.0")
    _write_skill(root, "explicit-skill", "1.0.0")
    loader = SkillLoader(root)
    registry = SkillRegistry(
        [
            loader.inspect("auto-skill"),
            loader.inspect("always-skill"),
            loader.inspect("explicit-skill"),
        ]
    )
    return SkillRuntime(registry=registry, loader=loader), registry


def test_registry_resolves_versions_and_rejects_duplicate_assignments(tmp_path: Path) -> None:
    _, registry = _runtime(tmp_path / "skills")

    assert registry.get("auto-skill").version == "1.0.0"
    assert registry.get("always-skill", "2.0.0").name == "always-skill"
    with pytest.raises(SkillNotFoundError):
        registry.get("auto-skill", "9.0.0")
    with pytest.raises(RegistryError, match="more than once"):
        registry.resolve_for_agent([AgentSkillRef(name="auto-skill"), AgentSkillRef(name="auto-skill")])

    registry.disable("auto-skill")
    with pytest.raises(SkillDisabledError):
        registry.get("auto-skill")


def test_registry_uses_semver_defaults_and_handles_legacy_versions(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "versioned-skill", "1.0.0")
    base = SkillLoader(root).inspect("versioned-skill")
    registry = SkillRegistry(
        [
            base.model_copy(update={"version": "legacy"}),
            base.model_copy(update={"version": "1.2.0"}),
            base.model_copy(update={"version": "1.10.0"}),
        ]
    )

    assert registry.get("versioned-skill").version == "1.10.0"
    assert [skill.version for skill in registry.list()] == ["legacy", "1.2.0", "1.10.0"]


def test_session_preloads_always_and_enforces_activation_and_resources(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path / "skills")
    refs = [
        AgentSkillRef(name="auto-skill"),
        AgentSkillRef(name="always-skill", activation="always"),
        AgentSkillRef(name="explicit-skill", activation="explicit"),
    ]
    session = asyncio.run(
        runtime.create_session(
            run_id="run-1",
            agent=AgentSpec(name="skill-agent", route_tags=["skill.test"], skills=refs),
            snapshots=runtime.snapshots_for_agent(
                AgentSpec(name="skill-agent", route_tags=["skill.test"], skills=refs)
            ),
        )
    )

    assert [item["name"] for item in session.provenance()] == ["always-skill"]
    with pytest.raises(SkillResourceError, match="must be activated"):
        asyncio.run(session.read_text_resource("auto-skill", "references/guide.md"))
    with pytest.raises(SkillActivationDeniedError, match="requires explicit"):
        asyncio.run(session.activate("explicit-skill", reason="model selected"))
    activation = asyncio.run(session.activate("explicit-skill", reason="trusted code", explicit=True))
    assert activation.instructions == "# explicit-skill"
    assert asyncio.run(session.read_text_resource("explicit-skill", "references/guide.md")) == "explicit-skill guide\n"
    assert asyncio.run(session.activate("explicit-skill", reason="again", explicit=True)) is activation
    with pytest.raises(SkillScriptError, match="not declared"):
        asyncio.run(session.run_script("explicit-skill", "anything", {}))
    with pytest.raises(SkillNotAssignedError):
        asyncio.run(session.activate("unknown-skill", reason="nope", explicit=True))


def test_sessions_do_not_share_activation_state(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path / "skills")
    agent = AgentSpec(name="skill-agent", route_tags=["skill.test"], skills=[AgentSkillRef(name="auto-skill")])
    snapshots = runtime.snapshots_for_agent(agent)
    first = asyncio.run(runtime.create_session(run_id="run-1", agent=agent, snapshots=snapshots))
    second = asyncio.run(runtime.create_session(run_id="run-2", agent=agent, snapshots=snapshots))
    asyncio.run(first.activate("auto-skill", reason="first", explicit=True))

    assert first.provenance()
    assert second.provenance() == []


def test_catalog_truncation_audits_only_omitted_skills(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path / "skills")
    events: list[tuple[str, dict]] = []
    agent = AgentSpec(
        name="skill-agent",
        route_tags=["skill.test"],
        skills=[
            AgentSkillRef(name="auto-skill"),
            AgentSkillRef(name="always-skill"),
            AgentSkillRef(name="explicit-skill"),
        ],
    )
    session = asyncio.run(
        runtime.create_session(
            run_id="run-1",
            agent=agent,
            snapshots=runtime.snapshots_for_agent(agent),
            audit=lambda event, data: events.append((event, data)),
        )
    )
    header = (
        "Available skills are listed below. Load a skill only when the task matches its description.\n"
        "Use skill_load with the exact name before following that skill's workflow.\n\n"
    )
    prefix = "- auto-skill@1.0.0: "

    catalog = session.catalog_prompt(max_chars=len(header) + len(prefix) + 8)

    assert "auto-skill@1.0.0" in catalog
    assert events[-1] == (
        "skill_catalog_truncated",
        {
            "included": 1,
            "omitted": ["always-skill", "explicit-skill"],
            "budget": len(header) + len(prefix) + 8,
        },
    )


def test_agent_runtime_resolves_skills_before_invoking_agent(tmp_path: Path) -> None:
    skill_runtime, _ = _runtime(tmp_path / "skills")
    events: list = []
    store = SimpleNamespace(logs=SimpleNamespace(add=events.append))
    runtime = AgentRuntime(store=store, tool_gateway=None, skill_runtime=skill_runtime)
    agent = AgentSpec(
        name="echo-agent",
        route_tags=["skill.test"],
        runtime={"type": "echo"},
        skills=[AgentSkillRef(name="always-skill", activation="always")],
    )
    run = AgentRun(route_tag="skill.test", request_id="request-1", input={"value": 1})
    run.skill_snapshots = skill_runtime.snapshots_for_agent(agent)

    output = asyncio.run(runtime.run(agent, run))

    assert output["data"] == {"value": 1}
    assert [event.event_type for event in events] == [
        "skill_activated",
        "skills_resolved",
        "agent_started",
        "agent_succeeded",
    ]
    assert events[2].data["skills"] == [
        {
            "name": "always-skill",
            "version": "2.0.0",
            "content_hash": events[1].data["skills"][0]["content_hash"],
            "activation": "always",
        }
    ]


def test_skill_snapshot_hash_is_part_of_durable_stage_definition(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path / "skills")
    agent = AgentSpec(
        name="stage-agent",
        route_tags=["stage.test"],
        skills=[AgentSkillRef(name="auto-skill")],
    )
    session = asyncio.run(
        runtime.create_session(
            run_id="run-1",
            agent=agent,
            snapshots=runtime.snapshots_for_agent(agent),
        )
    )
    context = AgentContext(
        run_id="run-1",
        route_tag="stage.test",
        trace_id="trace-1",
        metadata={},
        files=[],
        agent=agent,
        tool_client=None,
        model_client=None,
        logger=None,
        file_client=None,
        state_client=SimpleNamespace(),
        skills=session,
    )

    async def execute(stage):
        return {"definition_version": stage.definition_version}

    result = asyncio.run(context.run_stage("review", {}, execute, definition_version="review-v1"))

    assert result["definition_version"] == f"review-v1|skills:{session.snapshot_hash()}"
