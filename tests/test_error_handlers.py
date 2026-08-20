from __future__ import annotations

import asyncio
from typing import Any

from conftest import make_store as SqliteRunStore

from domain import AgentSpec
from domain.enums import RunStatus
from framework.registry import AgentRegistry
from framework.tool.error_handlers import RunErrorHandlers, RunErrorInput, RunErrorResult
from interfaces.schemas import RunSubmitRequest
from orchestration.callback_service import CallbackService
from orchestration.manager import RunManager
from orchestration.scheduler import RunScheduler


def test_task_error_handlers_register_remove_and_return_none() -> None:
    handlers = RunErrorHandlers()

    async def recover(error_input: RunErrorInput) -> RunErrorResult | None:
        assert error_input.run_id == "TASK-error"
        return RunErrorResult(final_output={"handled": True}, include_error_in_output=False)

    handlers.set("agent_error", recover)
    assert handlers.registered_kinds == ["agent_error"]

    result = asyncio.run(handlers.handle("agent_error", _error_input()))
    assert result.final_output == {"handled": True}
    assert result.include_error_in_output is False

    handlers.remove("agent_error")
    assert handlers.registered_kinds == []
    assert asyncio.run(handlers.handle("agent_error", _error_input())) is None


def test_error_handler_recovers_agent_error_and_completes_queue(tmp_path) -> None:
    seen: list[RunErrorInput] = []

    async def recover(error_input: RunErrorInput) -> RunErrorResult:
        seen.append(error_input)
        return RunErrorResult(final_output={"fallback": "agent"}, include_error_in_output=True)

    manager, store = _manager(
        tmp_path,
        runtime=_Runtime(RuntimeError("agent exploded")),
        error_handlers={"agent_error": recover},
    )

    run = asyncio.run(_submit_and_run(manager, route_tag="error.test", request_id="agent-error"))

    assert run.status == RunStatus.SUCCEEDED
    assert run.current_step == "error_recovery"
    assert run.output["fallback"] == "agent"
    assert run.output["_error"] == "RuntimeError: agent exploded"
    assert store.runs.get(run.run_id).status == RunStatus.SUCCEEDED
    assert seen[0].current_stage == "agent"
    assert seen[0].attempts == 1


def test_error_handler_recovers_timeout(tmp_path) -> None:
    async def recover(error_input: RunErrorInput) -> RunErrorResult:
        assert isinstance(error_input.error, asyncio.TimeoutError)
        assert error_input.agent_name == "error-agent"
        return RunErrorResult(final_output={"fallback": "timeout"}, include_error_in_output=False)

    manager, store = _manager(
        tmp_path,
        runtime=_Runtime(TimeoutError("late")),
        error_handlers={"timeout": recover},
    )

    run = asyncio.run(_submit_and_run(manager, route_tag="error.test", request_id="timeout"))

    assert run.status == RunStatus.SUCCEEDED
    assert run.output == {"fallback": "timeout"}
    assert store.runs.get(run.run_id).status == RunStatus.SUCCEEDED


def test_error_handler_recovers_output_validation_error(tmp_path) -> None:
    async def recover(error_input: RunErrorInput) -> RunErrorResult:
        assert error_input.current_stage == "agent"
        assert "validation failed" in str(error_input.error)
        return RunErrorResult(final_output={"fallback": "validation"}, include_error_in_output=False)

    manager, _store = _manager(
        tmp_path,
        runtime=_Runtime(output={"unexpected": True}),
        output_schema={
            "type": "object",
            "required": ["required_field"],
            "properties": {"required_field": {"type": "string"}},
            "additionalProperties": False,
        },
        error_handlers={"validation_error": recover},
    )

    run = asyncio.run(_submit_and_run(manager, route_tag="error.test", request_id="validation"))

    assert run.status == RunStatus.SUCCEEDED
    assert run.output == {"fallback": "validation"}


def test_error_handler_recovers_tool_failure_kind(tmp_path) -> None:
    async def recover(error_input: RunErrorInput) -> RunErrorResult:
        assert "tool" in str(error_input.error).lower()
        return RunErrorResult(final_output={"fallback": "tool"}, include_error_in_output=False)

    manager, _store = _manager(
        tmp_path,
        runtime=_Runtime(RuntimeError("tool call failed")),
        error_handlers={"tool_failure": recover},
    )

    run = asyncio.run(_submit_and_run(manager, route_tag="error.test", request_id="tool"))

    assert run.status == RunStatus.SUCCEEDED
    assert run.output == {"fallback": "tool"}


def test_error_handler_returning_none_preserves_failure_state(tmp_path) -> None:
    async def do_not_recover(_error_input: RunErrorInput) -> None:
        return None

    manager, store = _manager(
        tmp_path,
        runtime=_Runtime(RuntimeError("agent still failed")),
        error_handlers={"agent_error": do_not_recover},
    )

    run = asyncio.run(_submit_and_run(manager, route_tag="error.test", request_id="no-recovery"))

    assert run.status == RunStatus.FAILED
    assert run.error_type == "AGENT_EXECUTION_ERROR"
    assert store.runs.get(run.run_id).dead_letter_reason is not None


class _Runtime:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        output: dict[str, Any] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.error = error
        self.output = output or {"ok": True}
        self.delay_seconds = delay_seconds

    async def run(self, _agent: AgentSpec, _run) -> dict[str, Any]:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error:
            raise self.error
        return self.output


def _manager(
    tmp_path,
    *,
    runtime: _Runtime,
    error_handlers: dict[str, Any],
    agent_timeout_seconds: float = 30,
    output_schema: dict[str, Any] | None = None,
) -> tuple[RunManager, SqliteRunStore]:
    store = SqliteRunStore(tmp_path / "runs.db")
    agent = AgentSpec(
        name="error-agent",
        version="1.0.0",
        route_tags=["error.test"],
        runtime={"type": "echo"},
        timeout_seconds=agent_timeout_seconds,
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        output_schema=output_schema or {"type": "object"},
    )
    handlers = RunErrorHandlers()
    for kind, handler in error_handlers.items():
        handlers.set(kind, handler)
    return (
        RunManager(
            store=store,
            agent_registry=AgentRegistry([agent]),
            runtime=runtime,
            scheduler=RunScheduler(store=store),
            callback_service=CallbackService(store=store),
            auto_start=False,
            error_handlers=handlers,
        ),
        store,
    )


async def _submit_and_run(manager: RunManager, *, route_tag: str, request_id: str):
    submitted = await manager.submit(
        RunSubmitRequest(
            route_tag=route_tag,
            request_id=request_id,
            input={"value": request_id},
            caller="tester",
        )
    )
    return await manager.run_now(submitted.run_id)


def _error_input() -> RunErrorInput:
    return RunErrorInput(
        error=RuntimeError("boom"),
        run_id="TASK-error",
        trace_id="TRACE-error",
        route_tag="error.test",
    )
