from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from conftest import make_store

from domain import AgentSkillRef, AgentSpec, RunSubmission
from domain.enums import RunStatus
from framework.registry import AgentRegistry, SkillRegistry, ToolRegistry
from framework.runtime import AgentRuntime
from framework.skill import SkillLoader, SkillValidationError
from framework.skill.runtime import SkillRuntime
from interfaces.http.server import _load_skill_registry
from orchestration.callback_service import CallbackService
from orchestration.config_watcher import ConfigWatcher
from orchestration.manager import RunManager
from orchestration.run_service import RunService
from orchestration.scheduler import RunScheduler, SchedulerLimits


def _write_skill(root: Path, name: str = "review-playbook", version: str = "1.0.0") -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Apply this review playbook to compliance cases.\n"
        "metadata:\n"
        f'  version: "{version}"\n'
        "---\n\n"
        "# Review playbook\n",
        encoding="utf-8",
    )


def _agent() -> AgentSpec:
    return AgentSpec(
        name="skill-agent",
        version="1.0.0",
        route_tags=["skill.test"],
        runtime={"type": "echo"},
        skills=[AgentSkillRef(name="review-playbook", version="1.0.0", activation="always")],
    )


def test_skill_config_and_agent_refs_round_trip_through_db(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    spec = loader.inspect("review-playbook").model_copy(update={"owner": "compliance"})

    store.skills.save(spec)
    store.agents.save(_agent())

    assert store.skills.list() == [spec]
    assert store.agents.list()[0].skills == _agent().skills


def test_skill_seed_uses_artifact_metadata_and_preserves_existing_db_row(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    seed = tmp_path / "skills.yaml"
    seed.write_text("skills:\n  - source_path: review-playbook\n", encoding="utf-8")

    registry = _load_skill_registry(seed, store, loader=loader)
    seeded = registry.get("review-playbook", "1.0.0")
    assert seeded.description == "Apply this review playbook to compliance cases."

    store.skills.save(seeded.model_copy(update={"enabled": False, "managed_by": "db"}))
    registry = _load_skill_registry(seed, store, loader=loader)
    persisted = registry.get_optional("review-playbook", "1.0.0")
    assert persisted is not None
    assert not persisted.enabled
    assert persisted.managed_by == "db"


def test_submission_persists_verified_skill_snapshot_before_execution(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    skill = loader.inspect("review-playbook")
    store.skills.save(skill)
    runtime = SkillRuntime(registry=SkillRegistry([skill]), loader=loader)
    agent = _agent()

    run, created = RunService(store, AgentRegistry([agent]), runtime).submit(
        RunSubmission(
            route_tag="skill.test",
            request_id="snapshot-request",
        )
    )

    assert created
    assert run.skill_snapshots[0].model_dump() == {
        "name": "review-playbook",
        "version": "1.0.0",
        "content_hash": skill.content_hash,
        "activation": "always",
        "artifact_id": "review-playbook",
    }
    assert store.runs.get(run.run_id).skill_snapshots == run.skill_snapshots


def test_pinned_snapshot_rejects_registry_artifact_drift(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    original = loader.inspect("review-playbook")
    runtime = SkillRuntime(registry=SkillRegistry([original]), loader=loader)
    agent = _agent()
    snapshots = runtime.snapshots_for_agent(agent)
    drifted = original.model_copy(update={"content_hash": "different"})
    runtime = SkillRuntime(registry=SkillRegistry([drifted]), loader=loader)

    with pytest.raises(SkillValidationError, match="no longer matches"):
        asyncio.run(
            runtime.create_session(
                run_id="run-1",
                agent=agent,
                snapshots=snapshots,
            )
        )


def test_watcher_reloads_verified_skill_governance_without_mutating_snapshots(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    skill = loader.inspect("review-playbook")
    registry = SkillRegistry([skill])
    store.skills.save(skill)
    watcher = ConfigWatcher(
        config_dir=tmp_path,
        agent_registry=AgentRegistry([]),
        tool_registry=ToolRegistry([]),
        skill_registry=registry,
        skill_loader=loader,
        store=store,
    )

    assert watcher._reload_skills() == 0
    store.skills.save(skill.model_copy(update={"enabled": False}))
    assert watcher._reload_skills() == 1
    persisted = registry.get_optional("review-playbook", "1.0.0")
    assert persisted is not None
    assert not persisted.enabled


def test_watcher_loads_db_artifact_after_local_seed_is_removed(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    seeded = loader.inspect("review-playbook")
    db_managed = seeded.model_copy(update={"source_path": "db://review-playbook@1.0.0"})
    store.skills.save(db_managed)
    shutil.rmtree(root / "review-playbook")
    registry = SkillRegistry([])
    watcher = ConfigWatcher(
        config_dir=tmp_path,
        agent_registry=AgentRegistry([]),
        tool_registry=ToolRegistry([]),
        skill_registry=registry,
        skill_loader=loader,
        store=store,
    )

    assert watcher._reload_skills() == 1
    loaded = registry.get("review-playbook", "1.0.0")
    assert loader.load_instructions(loaded) == "# Review playbook"


def test_recovery_uses_pinned_snapshot_after_skill_is_disabled(tmp_path: Path) -> None:
    store = make_store(tmp_path / "runs.db")
    root = tmp_path / "skills"
    _write_skill(root)
    loader = SkillLoader(root)
    skill = loader.inspect("review-playbook")
    registry = SkillRegistry([skill])
    agent = _agent()
    runtime = AgentRuntime(
        store=store,
        tool_gateway=None,
        skill_runtime=SkillRuntime(registry=registry, loader=loader),
    )
    manager = RunManager(
        store=store,
        agent_registry=AgentRegistry([agent]),
        runtime=runtime,
        scheduler=RunScheduler(store=store, limits=SchedulerLimits(global_max_concurrency=1)),
        callback_service=CallbackService(store=store),
        auto_start=False,
    )
    submitted = asyncio.run(
        manager.submit(
            RunSubmission(
                route_tag="skill.test",
                request_id="recover-with-snapshot",
            )
        )
    )
    running = store.runs.get(submitted.run_id)
    running.status = RunStatus.RUNNING
    running.attempts = 1
    store.runs.update(running)
    manager._start_background = lambda _run_id: True

    assert manager.recover_incomplete() == 1
    registry.disable("review-playbook", "1.0.0")

    completed = asyncio.run(manager.run_now(submitted.run_id))

    assert completed.status == RunStatus.SUCCEEDED
