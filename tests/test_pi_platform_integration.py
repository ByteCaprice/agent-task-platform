"""Real PostgreSQL + RunExecutor + Pi + ToolGateway contract (requires Docker and npm Pi)."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import make_store
from test_pi_runtime import model_server, tool_call

from domain import AgentSpec, RunSubmission, ToolSpec
from domain.enums import RunStatus, StageStatus
from framework.registry import AgentRegistry, ToolRegistry
from framework.runtime import AgentRuntime
from framework.runtime.pi.transport import PiProcessError, runner_command
from framework.tool.tools import ToolGateway
from orchestration.callback_service import CallbackService
from orchestration.manager import RunManager
from orchestration.scheduler import RunScheduler


@pytest.mark.parametrize("succeed", [True, False])
def test_pi_whole_task_status_and_stage_reuse(succeed):
    try:
        runner_command()
    except PiProcessError as exc:
        pytest.skip(str(exc))
    responses = [{"calls": [tool_call()]}, {"text": '{"approved":true}'}] if succeed else [{"http_error": 401}]
    with model_server(responses) as (url, requests, _):
        store = make_store()
        spec = AgentSpec(
            name="pi-integration",
            route_tags=["pi.integration"],
            tools=["lookup"],
            runtime={"type": "pi", "max_turns": 3},
            retry_policy={"max_attempts": 1},
            output_schema={"type": "object", "required": ["approved"]},
        )
        gateway = ToolGateway(
            registry=ToolRegistry(
                [
                    ToolSpec(
                        name="lookup",
                        endpoint={"protocol": "builtin", "handler": "builtin.echo"},
                        allowed_agents=[spec.name],
                    ),
                ]
            ),
            store=store,
        )
        registry = AgentRegistry([spec])
        runtime = AgentRuntime(
            store=store,
            tool_gateway=gateway,
            model_client=SimpleNamespace(
                defaults={
                    "base_url": url,
                    "model": "test-model",
                    "api_key": "synthetic-key",
                    "max_retries": 0,
                }
            ),
        )
        manager = RunManager(
            store=store,
            agent_registry=registry,
            runtime=runtime,
            scheduler=RunScheduler(store=store),
            callback_service=CallbackService(store=store),
            auto_start=False,
        )

        async def scenario():
            try:
                run = await manager.submit(
                    RunSubmission(
                        route_tag="pi.integration",
                        request_id="pi-integration-request",
                        input={},
                    )
                )
                completed = await manager.run_now(run.run_id)
                assert completed.status == (RunStatus.SUCCEEDED if succeed else RunStatus.FAILED)
                stage = store.stages.get(run.run_id, "pi-agent")
                assert stage.status == (StageStatus.SUCCEEDED if succeed else StageStatus.FAILED)
                assert len(store.model_calls.for_run(run.run_id)) == (2 if succeed else 1)
                if succeed:
                    assert await runtime.run(spec, completed) == completed.output
                    assert len(requests) == 2  # restored stage never starts Pi again
                    assert "tool_call_succeeded" in [event.event_type for event in store.logs.for_run(run.run_id)]
            finally:
                await gateway.http_client.aclose()
                await manager.shutdown()
                store.close()

        asyncio.run(scenario())
