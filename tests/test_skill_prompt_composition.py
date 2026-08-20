from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from domain import AgentSkillRef, AgentSpec
from framework.registry import SkillRegistry
from framework.runtime.adapters.model_gateway_adapter import ModelGatewayAgent
from framework.runtime.context import AgentContext
from framework.skill import SkillLoader
from framework.skill.catalog import compose_instructions
from framework.skill.runtime import SkillRuntime
from framework.skill.session import EmptySkillSession


class _ModelClient:
    def __init__(self) -> None:
        self.runtime: dict | None = None

    async def complete(self, **kwargs):
        self.runtime = kwargs["runtime"]
        return {"ok": True}


def test_composition_preserves_the_legacy_fingerprint_without_skills() -> None:
    composed = compose_instructions(base_instructions="Base instructions\n", session=EmptySkillSession())

    assert composed.instructions == "Base instructions\n"
    assert composed.fingerprint_content == "Base instructions\n"
    assert composed.provenance == []


def test_model_gateway_receives_always_skill_instructions_and_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "always-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: always-skill\ndescription: Always apply this test workflow.\nmetadata:\n  version: '1.0.0'\n---\n\n# Always\n",
        encoding="utf-8",
    )
    loader = SkillLoader(root)
    runtime = SkillRuntime(registry=SkillRegistry([loader.inspect("always-skill")]), loader=loader)
    agent = AgentSpec(
        name="model-agent",
        route_tags=["model.test"],
        skills=[AgentSkillRef(name="always-skill", activation="always")],
    )
    session = asyncio.run(
        runtime.create_session(
            run_id="run-1",
            agent=agent,
            snapshots=runtime.snapshots_for_agent(agent),
        )
    )
    model_client = _ModelClient()
    context = AgentContext(
        run_id="run-1",
        route_tag="model.test",
        trace_id="trace-1",
        metadata={},
        files=[],
        agent=agent,
        tool_client=None,
        model_client=model_client,
        logger=None,
        file_client=None,
        state_client=SimpleNamespace(),
        skills=session,
    )

    assert asyncio.run(ModelGatewayAgent({"instructions": "Base instructions"}).run(context, {})) == {"ok": True}
    assert model_client.runtime is not None
    assert "Base instructions" in model_client.runtime["instructions"]
    assert "# Always" in model_client.runtime["instructions"]
    assert "always-skill" in model_client.runtime["prompt_fingerprint_content"]
