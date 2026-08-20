from __future__ import annotations

import asyncio

import pytest
from conftest import make_store

from domain import AgentRef, AgentRun, AgentSpec, AgentStage
from domain.enums import RunStatus, StageStatus
from framework.runtime import AgentRuntime, RuntimeStateClient, StageStateError
from framework.runtime.errors import ExternalSideEffectOutcomeUnknownError


class _RecoveringAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.review_attempts = 0

    async def run(self, context, input_data):
        async def extract(stage):
            self.calls.append("extract")
            return {"value": stage.stage_input["value"] * 2}

        extracted = await context.run_stage("extract", input_data, extract)

        async def review(stage):
            self.calls.append("review")
            self.review_attempts += 1
            if self.review_attempts == 1:
                await stage.save_checkpoint({"cursor": 7})
                raise RuntimeError("pause after checkpoint")
            assert stage.checkpoint == {"cursor": 7}
            return {"approved": True, "value": stage.stage_input["value"]}

        reviewed = await context.run_stage("review", extracted, review)
        return {"extracted": extracted, "reviewed": reviewed}


class _SingleStageAgent:
    async def run(self, context, input_data):
        async def execute(stage):
            return {"value": stage.stage_input["value"]}

        return await context.run_stage("only", input_data, execute)


class _BlockingStageAgent:
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    async def run(self, context, input_data):
        async def block(_stage):
            self.started.set()
            await asyncio.Event().wait()

        return await context.run_stage("block", input_data, block)


def test_durable_stage_retry_restores_completed_output_and_checkpoint(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    spec = AgentSpec(
        name="durable-agent",
        version="1.0.0",
        route_tags=["durable.test"],
        retry_policy={"max_attempts": 2, "backoff_seconds": 0},
        runtime={"type": "python", "target": "unused"},
    )
    run = _create_run(store, spec, {"value": 3})
    implementation = _RecoveringAgent()
    runtime = AgentRuntime(store=store, tool_gateway=None)
    runtime._load_agent = lambda _spec: implementation

    with pytest.raises(RuntimeError, match="pause after checkpoint"):
        asyncio.run(runtime.run(spec, run))

    first_pass = store.stages.list_for_run(run.run_id)
    assert [stage.status for stage in first_pass] == [
        StageStatus.SUCCEEDED,
        StageStatus.FAILED,
    ]
    assert first_pass[1].checkpoint == {"cursor": 7}

    run = store.runs.get(run.run_id)
    run.attempts = 2
    store.runs.update(run)
    output = asyncio.run(runtime.run(spec, run))

    assert output == {
        "extracted": {"value": 6},
        "reviewed": {"approved": True, "value": 6},
    }
    assert implementation.calls == ["extract", "review", "review"]
    completed = store.stages.list_for_run(run.run_id)
    assert [stage.attempts for stage in completed] == [1, 2]
    assert all(stage.status == StageStatus.SUCCEEDED for stage in completed)
    assert "stage_resumed" in [event.event_type for event in store.logs.for_run(run.run_id)]


def test_durable_stage_rejects_changed_input(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    spec = AgentSpec(
        name="durable-agent",
        route_tags=["durable.test"],
        runtime={"type": "python", "target": "unused"},
    )
    run = _create_run(store, spec, {"value": 1})
    runtime = AgentRuntime(store=store, tool_gateway=None)
    runtime._load_agent = lambda _spec: _SingleStageAgent()

    assert asyncio.run(runtime.run(spec, run)) == {"value": 1}
    run = store.runs.get(run.run_id)
    run.input = {"value": 2}

    with pytest.raises(StageStateError, match="input changed"):
        asyncio.run(runtime.run(spec, run))


def test_stage_execution_token_rejects_stale_worker_result(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    spec = AgentSpec(
        name="durable-agent",
        route_tags=["durable.test"],
        runtime={"type": "echo"},
    )
    run = _create_run(store, spec, {})
    stage = store.stages.get_or_create(
        AgentStage(
            run_id=run.run_id,
            trace_id=run.trace_id,
            agent_name=spec.name,
            agent_version=spec.version,
            stage_key="side-effect",
            stage_index=0,
            max_attempts=2,
            idempotency_key=f"{run.run_id}:side-effect",
            input_hash="hash",
        )
    )

    first = store.stages.begin_attempt(
        run.run_id,
        stage.stage_key,
        run_attempt=1,
        execution_id="worker-1",
    )
    second = store.stages.begin_attempt(
        run.run_id,
        stage.stage_key,
        run_attempt=1,
        execution_id="worker-2",
    )

    assert first is not None
    assert second is not None
    assert second.attempts == 1
    assert not store.stages.mark_succeeded(
        run.run_id,
        stage.stage_key,
        execution_id="worker-1",
        output={"worker": 1},
    )
    assert store.stages.mark_succeeded(
        run.run_id,
        stage.stage_key,
        execution_id="worker-2",
        output={"worker": 2},
    )
    saved = store.stages.get(run.run_id, stage.stage_key)
    assert saved.status == StageStatus.SUCCEEDED
    assert saved.output == {"worker": 2}


def test_unknown_side_effect_stage_is_not_replayed(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    spec = AgentSpec(
        name="durable-agent",
        route_tags=["durable.test"],
        retry_policy={"max_attempts": 2, "backoff_seconds": 0},
        runtime={"type": "python", "target": "unused"},
    )
    run = _create_run(store, spec, {})
    calls = {"count": 0}

    class UnknownSideEffectAgent:
        async def run(self, context, input_data):
            async def execute(_stage):
                calls["count"] += 1
                raise ExternalSideEffectOutcomeUnknownError("provider response was lost")

            return await context.run_stage("side-effect", input_data, execute)

    runtime = AgentRuntime(store=store, tool_gateway=None)
    runtime._load_agent = lambda _spec: UnknownSideEffectAgent()

    with pytest.raises(ExternalSideEffectOutcomeUnknownError):
        asyncio.run(runtime.run(spec, run))
    with pytest.raises(ExternalSideEffectOutcomeUnknownError):
        asyncio.run(runtime.run(spec, run))

    assert calls["count"] == 1
    assert store.stages.get(run.run_id, "side-effect").status == StageStatus.OUTCOME_UNKNOWN


def test_side_effect_stage_requires_recorded_response_before_success(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    spec = AgentSpec(name="durable-agent", route_tags=["durable.test"], runtime={"type": "echo"})
    run = _create_run(store, spec, {})
    stage = store.stages.get_or_create(
        AgentStage(
            run_id=run.run_id,
            trace_id=run.trace_id,
            agent_name=spec.name,
            agent_version=spec.version,
            stage_key="side-effect",
            stage_index=0,
            idempotency_key=f"{run.run_id}:side-effect",
            input_hash="hash",
        )
    )
    store.stages.begin_attempt(
        run.run_id,
        stage.stage_key,
        run_attempt=1,
        execution_id="execution-1",
    )
    assert store.stages.mark_side_effect_dispatched(
        run.run_id,
        idempotency_key=stage.idempotency_key,
        execution_id="execution-1",
    )
    assert not store.stages.mark_succeeded(
        run.run_id,
        stage.stage_key,
        execution_id="execution-1",
        output={"masked": "failure"},
    )
    assert store.stages.mark_side_effect_returned(
        run.run_id,
        idempotency_key=stage.idempotency_key,
        execution_id="execution-1",
    )
    assert store.stages.mark_succeeded(
        run.run_id,
        stage.stage_key,
        execution_id="execution-1",
        output={"ok": True},
    )


def test_canceled_runtime_keeps_stage_recoverable_and_closes_lifecycle(tmp_path) -> None:
    async def scenario() -> None:
        store = make_store(tmp_path / "runs.db")
        spec = AgentSpec(
            name="durable-agent",
            route_tags=["durable.test"],
            runtime={"type": "python", "target": "unused"},
        )
        run = _create_run(store, spec, {})
        started = asyncio.Event()
        runtime = AgentRuntime(store=store, tool_gateway=None)
        runtime._load_agent = lambda _spec: _BlockingStageAgent(started)
        task = asyncio.create_task(runtime.run(spec, run))
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        stage = store.stages.get(run.run_id, "block")
        assert stage.status == StageStatus.RUNNING
        event_types = [event.event_type for event in store.logs.for_run(run.run_id)]
        assert "stage_interrupted" in event_types
        assert "agent_canceled" in event_types

    asyncio.run(scenario())


def test_agent_runtime_uses_explicit_empty_cancellation_registry() -> None:
    cancellation_events = {}

    runtime = AgentRuntime(
        store=None,
        tool_gateway=None,
        cancellation_events=cancellation_events,
    )

    assert runtime._cancellation_events is cancellation_events


def test_runtime_state_client_exposes_narrow_store_capabilities(tmp_path) -> None:
    store = make_store(tmp_path / "runs.db")
    client = RuntimeStateClient(store)

    assert client.get_prompt_spec("missing") is None
    assert not hasattr(client, "runs")
    assert not hasattr(client, "tools")


def _create_run(store, spec: AgentSpec, input_data: dict) -> AgentRun:
    run = AgentRun(
        route_tag=spec.route_tags[0],
        caller="pytest",
        request_id=f"request-{len(store.runs.list())}",
        input=input_data,
        agent=AgentRef(name=spec.name, version=spec.version),
        status=RunStatus.RUNNING,
        attempts=1,
        max_attempts=spec.retry_policy.max_attempts,
    )
    store.runs.create(run)
    return run
