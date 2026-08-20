"""Run lifecycle hooks: the no-op ``RunHooks`` base, ``CompositeHooks`` (fans out
to several hooks), and ``LoggingHooks`` (writes structured log events), plus the
``HookContext`` payload passed to every callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infra.store import RunStore


@dataclass
class HookContext:
    run_id: str
    trace_id: str
    route_tag: str
    caller: str = ""
    agent_name: str = ""
    agent_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class RunHooks:
    """Lifecycle callbacks for agent run execution.

    Subclass and override the methods you need. Every method is a no-op by default.
    """

    async def on_run_enqueued(self, context: HookContext) -> None:
        """Called after a run is accepted and enqueued (status == QUEUED)."""
        pass

    async def on_run_dequeued(self, context: HookContext) -> None:
        """Called when the scheduler admits a run for execution."""
        pass

    async def on_run_end(
        self,
        context: HookContext,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Called when run execution finishes (success, failure, timeout, or cancel)."""
        pass

    async def on_agent_start(self, context: HookContext, input_data: dict[str, Any]) -> None:
        """Called before the agent implementation is invoked."""
        pass

    async def on_agent_end(self, context: HookContext, output: dict[str, Any]) -> None:
        """Called after the agent implementation returns successfully."""
        pass

    async def on_agent_error(self, context: HookContext, error: BaseException) -> None:
        """Called after the agent implementation raises an error."""
        pass

    async def on_stage_start(self, context: Any) -> None:
        """Called after a durable stage attempt is persisted as running."""
        pass

    async def on_stage_end(
        self,
        context: Any,
        output: Any = None,
        error: BaseException | None = None,
    ) -> None:
        """Called after a durable stage attempt succeeds or fails."""
        pass

    async def on_tool_start(self, context: HookContext, tool_name: str, input_data: dict[str, Any]) -> None:
        """Called immediately before a tool is invoked."""
        pass

    async def on_tool_end(
        self,
        context: HookContext,
        tool_name: str,
        output: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Called after a tool invocation completes (success or failure)."""
        pass

    async def on_llm_start(self, context: HookContext, model: str, prompt_summary: str) -> None:
        """Called immediately before an LLM call."""
        pass

    async def on_llm_end(
        self,
        context: HookContext,
        model: str,
        output_summary: str,
        usage: dict[str, int],
    ) -> None:
        """Called after an LLM call returns."""
        pass

    async def on_callback_start(self, context: HookContext, callback_url: str) -> None:
        """Called before a callback is dispatched."""
        pass

    async def on_callback_end(
        self,
        context: HookContext,
        callback_url: str,
        success: bool,
        error: Exception | None = None,
    ) -> None:
        """Called after a callback dispatch attempt."""
        pass


class CompositeHooks(RunHooks):
    """Runs a list of hooks in order."""

    def __init__(self, hooks: list[RunHooks] | None = None) -> None:
        self._hooks: list[RunHooks] = list(hooks or [])

    def add(self, hook: RunHooks) -> None:
        self._hooks.append(hook)

    async def on_run_enqueued(self, context: HookContext) -> None:
        for h in self._hooks:
            await h.on_run_enqueued(context)

    async def on_run_dequeued(self, context: HookContext) -> None:
        for h in self._hooks:
            await h.on_run_dequeued(context)

    async def on_run_end(
        self,
        context: HookContext,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        for h in self._hooks:
            await h.on_run_end(context, result=result, error=error)

    async def on_agent_start(self, context: HookContext, input_data: dict[str, Any]) -> None:
        for h in self._hooks:
            await h.on_agent_start(context, input_data)

    async def on_agent_end(self, context: HookContext, output: dict[str, Any]) -> None:
        for h in self._hooks:
            await h.on_agent_end(context, output)

    async def on_agent_error(self, context: HookContext, error: BaseException) -> None:
        for h in self._hooks:
            await h.on_agent_error(context, error)

    async def on_stage_start(self, context: Any) -> None:
        for h in self._hooks:
            await h.on_stage_start(context)

    async def on_stage_end(
        self,
        context: Any,
        output: Any = None,
        error: BaseException | None = None,
    ) -> None:
        for h in self._hooks:
            await h.on_stage_end(context, output=output, error=error)

    async def on_tool_start(self, context: HookContext, tool_name: str, input_data: dict[str, Any]) -> None:
        for h in self._hooks:
            await h.on_tool_start(context, tool_name, input_data)

    async def on_tool_end(
        self,
        context: HookContext,
        tool_name: str,
        output: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        for h in self._hooks:
            await h.on_tool_end(context, tool_name, output=output, error=error)

    async def on_llm_start(self, context: HookContext, model: str, prompt_summary: str) -> None:
        for h in self._hooks:
            await h.on_llm_start(context, model, prompt_summary)

    async def on_llm_end(
        self,
        context: HookContext,
        model: str,
        output_summary: str,
        usage: dict[str, int],
    ) -> None:
        for h in self._hooks:
            await h.on_llm_end(context, model, output_summary, usage)

    async def on_callback_start(self, context: HookContext, callback_url: str) -> None:
        for h in self._hooks:
            await h.on_callback_start(context, callback_url)

    async def on_callback_end(
        self,
        context: HookContext,
        callback_url: str,
        success: bool,
        error: Exception | None = None,
    ) -> None:
        for h in self._hooks:
            await h.on_callback_end(context, callback_url, success, error)


class LoggingHooks(RunHooks):
    """Default hook that writes structured log events via the store."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def _log(self, context: HookContext, component: str, event_type: str, message: str, **data: Any) -> None:
        from domain import LogEvent

        self._store.logs.add(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component=component,
                event_type=event_type,
                message=message,
                data=data,
            )
        )

    async def on_run_enqueued(self, context: HookContext) -> None:
        self._log(
            context,
            "run_api",
            "run_enqueued",
            "Task enqueued",
            route_tag=context.route_tag,
            agent=context.agent_name,
            status="QUEUED",
        )

    async def on_run_dequeued(self, context: HookContext) -> None:
        self._log(
            context,
            "scheduler",
            "run_dequeued",
            "Task admitted by scheduler",
            agent=context.agent_name,
            route_tag=context.route_tag,
            caller=context.caller,
        )

    async def on_run_end(
        self, context: HookContext, result: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        if error:
            self._log(context, "run_manager", "run_failed", "Task failed", error=f"{type(error).__name__}: {error}")
        else:
            self._log(
                context,
                "run_manager",
                "run_finished",
                "Task finished",
                result_summary=str(result)[:200] if result else None,
            )

    async def on_agent_start(self, context: HookContext, input_data: dict[str, Any]) -> None:
        self._log(
            context,
            "agent_runtime",
            "agent_started",
            f"Agent {context.agent_name}@{context.agent_version} started",
            agent=context.agent_name,
            version=context.agent_version,
        )

    async def on_agent_end(self, context: HookContext, output: dict[str, Any]) -> None:
        self._log(
            context,
            "agent_runtime",
            "agent_succeeded",
            f"Agent {context.agent_name}@{context.agent_version} succeeded",
            output_summary=str(output)[:200],
        )

    async def on_tool_start(self, context: HookContext, tool_name: str, input_data: dict[str, Any]) -> None:
        self._log(
            context, "tool_gateway", "tool_call_started", f"Calling tool {tool_name}", tool=tool_name, input=input_data
        )

    async def on_tool_end(
        self, context: HookContext, tool_name: str, output: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        if error:
            self._log(
                context,
                "tool_gateway",
                "tool_call_failed",
                f"Tool {tool_name} failed",
                tool=tool_name,
                error=f"{type(error).__name__}: {error}",
            )
        else:
            self._log(
                context,
                "tool_gateway",
                "tool_call_succeeded",
                f"Tool {tool_name} succeeded",
                tool=tool_name,
                output=output,
            )

    async def on_llm_start(self, context: HookContext, model: str, prompt_summary: str) -> None:
        self._log(context, "model_gateway", "llm_call_started", "LLM call started", model=model)

    async def on_llm_end(self, context: HookContext, model: str, output_summary: str, usage: dict[str, int]) -> None:
        self._log(context, "model_gateway", "llm_call_succeeded", "LLM call succeeded", model=model, usage=usage)

    async def on_callback_start(self, context: HookContext, callback_url: str) -> None:
        self._log(context, "callback_service", "callback_started", "Dispatching callback", url=callback_url)

    async def on_callback_end(
        self, context: HookContext, callback_url: str, success: bool, error: Exception | None = None
    ) -> None:
        if error:
            self._log(
                context,
                "callback_service",
                "callback_failed",
                "Callback dispatch failed",
                url=callback_url,
                error=f"{type(error).__name__}: {error}",
            )
        else:
            self._log(context, "callback_service", "callback_succeeded", "Callback delivered", url=callback_url)
