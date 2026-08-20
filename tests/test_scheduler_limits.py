from __future__ import annotations

import asyncio

from conftest import make_store as SqliteRunStore

from domain import AgentRun, AgentSpec
from orchestration.scheduler import RunScheduler, SchedulerLimits


def test_scheduler_enforces_global_route_tag_and_caller_limits(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    scheduler = RunScheduler(
        store=store,
        limits=SchedulerLimits(
            global_max_concurrency=1,
            route_tag_max_concurrency={"limited.tag": 1},
            caller_max_concurrency={"tester": 1},
        ),
    )
    agent = AgentSpec(
        name="limited-agent",
        version="1.0.0",
        route_tags=["limited.tag"],
        runtime={"type": "echo"},
        max_concurrency=1,
    )
    running = 0
    max_running = 0
    started = asyncio.Event()

    async def call() -> AgentRun:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        started.set()
        await asyncio.sleep(0.02)
        running -= 1
        return AgentRun(route_tag="limited.tag", request_id="done")

    async def run_two() -> None:
        runs = [
            AgentRun(
                run_id=f"TASK-limit-{index}",
                trace_id=f"TRACE-limit-{index}",
                route_tag="limited.tag",
                caller="tester",
                request_id=str(index),
            )
            for index in range(2)
        ]
        await asyncio.gather(*(scheduler.run_with_limits(run=run, agent=agent, call=call) for run in runs))

    asyncio.run(run_two())

    assert started.is_set()
    assert max_running == 1
    assert scheduler.metrics()["running_by_route_tag"] == {"limited.tag": 0}
    logs = store.logs._list_logs()
    assert [event.event_type for event in logs] == ["run_dequeued", "run_dequeued"]
