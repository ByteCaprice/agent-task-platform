import asyncio
import base64
from types import SimpleNamespace

import pytest

from domain import AgentRef, AgentRun, AgentSkillRef, AgentSpec, SkillArtifactFile, SkillSpec
from domain.enums import RunStatus
from framework.runtime.context import AgentContext
from framework.runtime.stage_runner import StageRunner, StageStateError
from framework.skill.errors import SkillActivationDeniedError
from framework.skill.loader import SkillLoader
from framework.skill.scripts import SkillScriptRunner
from framework.skill.session import SkillSession


class MockStore:
    def __init__(self):
        self._runs = {}
        self._stages = {}
        self._logs = []
        self.runs = SimpleNamespace(
            create=lambda run, **kw: self._runs.setdefault(run.run_id, run),
            get=lambda run_id: self._runs.get(run_id),
            update=lambda run, **kw: self._runs.update({run.run_id: run}),
            update_if_current=lambda run, **kw: self._runs.update({run.run_id: run}) or True,
            list_by_run=lambda run_id: [r for r in self._runs.values() if r.run_id == run_id],
        )
        self.stages = SimpleNamespace(
            get_or_create=lambda stage, **kw: self._stages.setdefault((stage.run_id, stage.stage_key), stage),
            get=lambda run_id, stage_key: self._stages.get((run_id, stage_key)),
            list_for_run=lambda run_id: [s for (r_id, _), s in self._stages.items() if r_id == run_id],
            list_by_run=lambda run_id: [s for (r_id, _), s in self._stages.items() if r_id == run_id],
            begin_attempt=lambda run_id, stage_key, **kw: self._stages.get((run_id, stage_key)),
            save_checkpoint=lambda run_id, stage_key, **kw: True,
            mark_succeeded=lambda run_id, stage_key, output, **kw: (
                (
                    setattr(self._stages.get((run_id, stage_key)), "output", output)
                    if self._stages.get((run_id, stage_key))
                    else None
                )
                or True
            ),
            mark_failed=lambda run_id, stage_key, **kw: True,
            mark_canceled=lambda run_id, stage_key, **kw: True,
            mark_outcome_unknown=lambda run_id, stage_key, **kw: True,
            reset_failed_for_run=lambda run_id, **kw: 0,
        )
        self.logs = SimpleNamespace(
            add=lambda event, **kw: self._logs.append(event),
            for_run=lambda run_id: [e for e in self._logs if getattr(e, "run_id", None) == run_id],
        )


def _make_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _make_skill_spec(
    name: str = "risk-evaluator",
    version: str = "1.0.0",
    script_code: str = "import sys, json\ndata = json.load(open(sys.argv[1]))\nprint(json.dumps({'score': data.get('val', 0) * 2}))",
) -> SkillSpec:
    files = [
        SkillArtifactFile(
            path="SKILL.md",
            content_base64=_make_b64(
                f'---\nname: {name}\ndescription: Evaluates risk for transactions\nmetadata:\n  version: "{version}"\n---\n# Risk Evaluator\n'
            ),
        ),
        SkillArtifactFile(
            path="scripts/eval.py",
            content_base64=_make_b64(script_code),
        ),
    ]
    loader = SkillLoader("/tmp")
    return loader.inspect_artifact(files, source_path=f"db://{name}@{version}")


def test_durable_stage_versioning_with_skill_snapshot() -> None:
    async def _run():
        store = MockStore()
        loader = SkillLoader("/tmp")
        spec1 = _make_skill_spec(version="1.0.0")
        spec2 = _make_skill_spec(version="1.1.0", script_code="print('updated')")

        agent_spec = AgentSpec(
            name="test-compliance-agent",
            version="1.0.0",
            route_tags=["TEST"],
            skills=[AgentSkillRef(name=spec1.name, version=spec1.version, activation="always")],
        )

        run = AgentRun(
            run_id="run-stage-test-1",
            request_id="req-1",
            tenant_id="example-tenant",
            user_id="u1",
            route_tag="TEST",
            agent=AgentRef(name=agent_spec.name, version=agent_spec.version),
            status=RunStatus.RUNNING,
        )
        store.runs.create(run)

        session1 = SkillSession(run_id=run.run_id, assignments=[(agent_spec.skills[0], spec1)], loader=loader)
        await session1.preload_always()

        runner1 = StageRunner(run=run, agent=agent_spec, store=store)
        ctx1 = AgentContext(
            run_id=run.run_id,
            route_tag="TEST",
            trace_id="tr-1",
            metadata={},
            files=[],
            agent=agent_spec,
            tool_client=None,
            model_client=None,
            logger=None,
            file_client=None,
            state_client=None,
            stage_runner=runner1,
            skills=session1,
        )

        # 1. Run stage under session 1
        async def step1(s_ctx):
            return {"result": "initial_output"}

        res1 = await ctx1.run_stage("risk_assessment", {"amount": 100}, step1, definition_version="v1")
        assert res1 == {"result": "initial_output"}

        # Stage definition version in store should contain skill snapshot hash
        stages = store.stages.list_by_run(run.run_id)
        assert len(stages) == 1
        assert "skills:" in stages[0].definition_version
        assert stages[0].definition_version.startswith("v1|skills:")

        # 2. Run stage on a recovery attempt under session 2 (different skill hash) -> should detect definition change
        agent_spec2 = agent_spec.model_copy(
            update={"skills": [AgentSkillRef(name=spec2.name, version=spec2.version, activation="always")]}
        )
        session2 = SkillSession(run_id=run.run_id, assignments=[(agent_spec2.skills[0], spec2)], loader=loader)
        await session2.preload_always()
        runner2 = StageRunner(run=run, agent=agent_spec, store=store)
        ctx2 = AgentContext(
            run_id=run.run_id,
            route_tag="TEST",
            trace_id="tr-1",
            metadata={},
            files=[],
            agent=agent_spec,
            tool_client=None,
            model_client=None,
            logger=None,
            file_client=None,
            state_client=None,
            stage_runner=runner2,
            skills=session2,
        )

        with pytest.raises(StageStateError, match="definition changed"):
            await ctx2.run_stage("risk_assessment", {"amount": 100}, step1, definition_version="v1")

    asyncio.run(_run())


def test_candidate_catalog_budget_and_deterministic_truncation() -> None:
    loader = SkillLoader("/tmp")
    events = []

    def audit(event_type: str, data: dict):
        events.append((event_type, data))

    specs = []
    assignments = []
    for i in range(5):
        s_name = f"skill-{i:02d}"
        desc = f"Specialized compliance workflow for rule {i:02d} with extensive guidelines."
        spec = loader.inspect_artifact(
            [
                SkillArtifactFile(
                    path="SKILL.md",
                    content_base64=_make_b64(
                        f'---\nname: {s_name}\ndescription: {desc}\nmetadata:\n  version: "1.0.0"\n---\n# {s_name}\n'
                    ),
                )
            ],
            source_path=f"db://{s_name}@1.0.0",
        )
        specs.append(spec)
        assignments.append((AgentSkillRef(name=s_name, version="1.0.0", activation="auto"), spec))

    session = SkillSession(run_id="run-catalog-test", assignments=assignments, loader=loader, audit=audit)

    # 1. Test full budget
    full_prompt = session.catalog_prompt(max_chars=8000)
    assert "skill-00@1.0.0" in full_prompt
    assert "skill-04@1.0.0" in full_prompt

    # 2. Test tight budget causing deterministic truncation
    tight_prompt = session.catalog_prompt(max_chars=250)
    assert len(tight_prompt) <= 250
    assert "skill-00@1.0.0" in tight_prompt
    assert any(ev[0] == "skill_catalog_truncated" for ev in events)

    # Check audit payload
    trunc_event = [ev[1] for ev in events if ev[0] == "skill_catalog_truncated"][0]
    assert trunc_event["budget"] == 250
    assert len(trunc_event["omitted"]) > 0


def test_safe_skill_script_runner_success() -> None:
    async def _run():
        loader = SkillLoader("/tmp")
        audit_logs = []
        spec = _make_skill_spec(
            script_code="""
import sys, json, os

# Verify inputs can be read from sys.argv[1], stdin, or env
input_data = {}
if len(sys.argv) > 1:
    with open(sys.argv[1], 'r') as f:
        input_data = json.load(f)

val = input_data.get("val", 0)
print(json.dumps({"computed_value": val * 10, "status": "OK"}))
"""
        )

        res = await SkillScriptRunner.run(
            spec,
            "eval",
            {"val": 42},
            loader=loader,
            audit=lambda ev, d: audit_logs.append((ev, d)),
        )

        assert res["success"] is True
        assert res["exit_code"] == 0
        assert res["result"] == {"computed_value": 420, "status": "OK"}
        assert any(log[0] == "skill_script_succeeded" for log in audit_logs)

    asyncio.run(_run())


def test_safe_skill_script_runner_error_resilience() -> None:
    async def _run():
        loader = SkillLoader("/tmp")
        audit_logs = []
        # Script that raises an unhandled exception and exits with error code 1
        spec = _make_skill_spec(
            script_code="""
import sys
sys.stderr.write("Division by zero error simulation\\n")
sys.exit(1)
"""
        )

        # Should NOT raise an exception or crash the main process/thread
        res = await SkillScriptRunner.run(
            spec,
            "eval",
            {},
            loader=loader,
            audit=lambda ev, d: audit_logs.append((ev, d)),
        )

        assert res["success"] is False
        assert res["exit_code"] == 1
        assert "Division by zero" in res["stderr"]
        assert res["error_type"] == "NON_ZERO_EXIT"
        assert any(log[0] == "skill_script_failed" for log in audit_logs)

    asyncio.run(_run())


def test_safe_skill_script_runner_timeout_protection() -> None:
    async def _run():
        loader = SkillLoader("/tmp")
        audit_logs = []
        # Script that attempts to sleep indefinitely
        spec = _make_skill_spec(
            script_code="""
import time
time.sleep(10)
"""
        )

        # Effective timeout of 0.3s
        res = await SkillScriptRunner.run(
            spec,
            "eval",
            {},
            loader=loader,
            timeout_seconds=0.3,
            audit=lambda ev, d: audit_logs.append((ev, d)),
        )

        assert res["success"] is False
        assert res["error_type"] == "TIMEOUT"
        assert "timed out" in res["error"]
        assert any(log[0] == "skill_script_failed" for log in audit_logs)

    asyncio.run(_run())


def test_skill_session_run_script_integration() -> None:
    async def _run():
        loader = SkillLoader("/tmp")
        spec = _make_skill_spec(
            name="sanctions-screener",
            script_code="""
import sys, json
data = json.load(open(sys.argv[1]))
country = data.get("country", "")
flagged = country in ["KP", "IR", "RU", "CU"]
print(json.dumps({"flagged": flagged, "risk_tier": "HIGH" if flagged else "LOW"}))
""",
        )
        ref = AgentSkillRef(name=spec.name, version=spec.version, activation="always")
        session = SkillSession(run_id="run-script-test", assignments=[(ref, spec)], loader=loader)
        await session.preload_always()

        # Active skill script execution
        res1 = await session.run_script("sanctions-screener", "eval", {"country": "RU"})
        assert res1["success"] is True
        assert res1["result"]["flagged"] is True
        assert res1["result"]["risk_tier"] == "HIGH"

        res2 = await session.run_script("sanctions-screener", "eval", {"country": "SG"})
        assert res2["success"] is True
        assert res2["result"]["flagged"] is False
        assert res2["result"]["risk_tier"] == "LOW"

    asyncio.run(_run())


def test_skill_activation_denied_is_audited() -> None:
    async def _run():
        loader = SkillLoader("/tmp")
        events: list[tuple[str, dict]] = []
        spec = _make_skill_spec(name="explicit-only")
        ref = AgentSkillRef(name=spec.name, version=spec.version, activation="explicit")
        session = SkillSession(
            run_id="run-denied",
            assignments=[(ref, spec)],
            loader=loader,
            audit=lambda event, data: events.append((event, data)),
        )
        with pytest.raises(SkillActivationDeniedError):
            await session.activate(spec.name, reason="model requested load")
        assert events[-1][0] == "skill_activation_denied"
        assert events[-1][1]["reason"] == "explicit_required"

    asyncio.run(_run())
