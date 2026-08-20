from __future__ import annotations

import asyncio

from conftest import make_store as SqliteRunStore

from framework.tool.hooks import CompositeHooks, HookContext, LoggingHooks, RunHooks


class _RecordingHook(RunHooks):
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def on_agent_start(self, context: HookContext, input_data: dict) -> None:
        self.calls.append(f"{self.name}:agent_start:{context.run_id}:{input_data['value']}")

    async def on_tool_end(self, context: HookContext, tool_name: str, output=None, error=None) -> None:
        self.calls.append(f"{self.name}:tool_end:{tool_name}:{output['ok']}")


def test_composite_hooks_run_in_registration_order() -> None:
    calls: list[str] = []
    hooks = CompositeHooks([_RecordingHook("first", calls)])
    hooks.add(_RecordingHook("second", calls))
    context = _context()

    asyncio.run(hooks.on_agent_start(context, {"value": 7}))
    asyncio.run(hooks.on_tool_end(context, "tool-a", output={"ok": True}))

    assert calls == [
        "first:agent_start:TASK-hook:7",
        "second:agent_start:TASK-hook:7",
        "first:tool_end:tool-a:True",
        "second:tool_end:tool-a:True",
    ]


def test_logging_hooks_write_structured_events(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    hooks = LoggingHooks(store)
    context = _context()

    asyncio.run(hooks.on_agent_start(context, {"value": 1}))
    asyncio.run(hooks.on_tool_end(context, "tool-a", output={"ok": True}))
    asyncio.run(hooks.on_callback_end(context, "https://callback.test/result", success=True))

    logs = store.logs.for_run("TASK-hook")
    assert [event.event_type for event in logs] == [
        "agent_started",
        "tool_call_succeeded",
        "callback_succeeded",
    ]
    assert logs[0].data == {"agent": "hook-agent", "version": "1.0.0"}
    assert logs[1].data["tool"] == "tool-a"
    assert logs[2].data["url"] == "https://callback.test/result"


def _context() -> HookContext:
    return HookContext(
        run_id="TASK-hook",
        trace_id="TRACE-hook",
        route_tag="hook.test",
        caller="tester",
        agent_name="hook-agent",
        agent_version="1.0.0",
        metadata={"env": "test"},
    )
