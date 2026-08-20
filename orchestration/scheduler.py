"""RunScheduler: concurrency-limit admission control for runs, applying
global / per-route-tag / per-caller limits via local semaphores or a
distributed coordination backend."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from domain import AgentRun, AgentSpec, LogEvent
from infra.coordination import CoordinationBackend, LocalCoordinationBackend, coordination_limited_slot
from infra.store import RunStore


@dataclass(slots=True)
class SchedulerLimits:
    global_max_concurrency: int = 20
    route_tag_max_concurrency: dict[str, int] | None = None
    caller_max_concurrency: dict[str, int] | None = None
    max_nested_depth: int = 3
    max_active_children_per_root: int = 5


class RunScheduler:
    def __init__(
        self,
        *,
        store: RunStore,
        limits: SchedulerLimits | None = None,
        coordination: CoordinationBackend | None = None,
    ) -> None:
        self.store = store
        self.limits = limits or SchedulerLimits()
        self.coordination = coordination or LocalCoordinationBackend()
        self.global_semaphore = asyncio.Semaphore(self.limits.global_max_concurrency)
        self.agent_semaphores: dict[str, asyncio.Semaphore] = {}
        self.task_tag_semaphores: dict[str, asyncio.Semaphore] = {}
        self.caller_semaphores: dict[str, asyncio.Semaphore] = {}
        self.nested_children_semaphores: dict[str, asyncio.Semaphore] = {}
        self.ancestor_reentrant_semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self.running_by_agent: defaultdict[str, int] = defaultdict(int)
        self.running_by_route_tag: defaultdict[str, int] = defaultdict(int)
        self.running_by_caller: defaultdict[str, int] = defaultdict(int)
        self._active_run_holders: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def semaphore_for_agent(self, agent: AgentSpec) -> asyncio.Semaphore:
        with self._lock:
            return self.agent_semaphores.setdefault(agent.name, asyncio.Semaphore(agent.max_concurrency))

    def semaphore_for_route_tag(self, route_tag: str) -> asyncio.Semaphore:
        limits = self.limits.route_tag_max_concurrency or {}
        with self._lock:
            return self.task_tag_semaphores.setdefault(route_tag, asyncio.Semaphore(limits.get(route_tag, 10_000)))

    def semaphore_for_caller(self, caller: str) -> asyncio.Semaphore:
        limits = self.limits.caller_max_concurrency or {}
        with self._lock:
            return self.caller_semaphores.setdefault(caller, asyncio.Semaphore(limits.get(caller, 10_000)))

    def semaphore_for_nested_children(self, root_run_id: str) -> asyncio.Semaphore:
        with self._lock:
            return self.nested_children_semaphores.setdefault(
                root_run_id,
                asyncio.Semaphore(self.limits.max_active_children_per_root),
            )

    def semaphore_for_ancestor_lane(self, key: tuple[str, str]) -> asyncio.Semaphore:
        with self._lock:
            return self.ancestor_reentrant_semaphores.setdefault(key, asyncio.Semaphore(1))

    async def run_with_limits(self, *, run: AgentRun, agent: AgentSpec, call) -> AgentRun:
        metadata = run.metadata or {}
        parent_run_id = metadata.get("parent_run_id")
        root_run_id = str(metadata.get("root_run_id") or run.run_id)
        call_depth = int(metadata.get("call_depth") or 0)

        # 1. Enforce max nested call depth limit
        if call_depth > self.limits.max_nested_depth:
            raise RuntimeError(
                f"Nested execution depth limit {self.limits.max_nested_depth} exceeded (call_depth={call_depth})"
            )

        is_child = bool(parent_run_id)

        # 2. Inspect active ancestor calling chain to exempt only active ancestors
        ancestor_agent_holders: dict[str, str] = {}
        ancestor_route_tag_holders: dict[str, str] = {}
        ancestor_callers: set[str] = set()
        with self._lock:
            curr_parent = parent_run_id
            while curr_parent:
                parent_holder = self._active_run_holders.get(curr_parent)
                if not parent_holder:
                    break
                ancestor_agent_holders.setdefault(parent_holder["agent"], curr_parent)
                ancestor_route_tag_holders.setdefault(parent_holder["route_tag"], curr_parent)
                ancestor_callers.add(parent_holder["caller"])
                curr_parent = parent_holder.get("parent_run_id")

        ancestor_agent_id = ancestor_agent_holders.get(agent.name)
        ancestor_route_id = ancestor_route_tag_holders.get(run.route_tag)
        caller_exempt = is_child and (run.caller in ancestor_callers)

        async with AsyncExitStack() as stack:
            # 3. Bound child concurrency under root run; root run acquires global capacity slot
            if is_child:
                await stack.enter_async_context(self.semaphore_for_nested_children(root_run_id))
            else:
                await stack.enter_async_context(self.global_semaphore)

            # 4. Acquire agent semaphore or serialize on the ancestor's single reentrant lane
            if ancestor_agent_id is not None:
                await stack.enter_async_context(
                    self.semaphore_for_ancestor_lane((ancestor_agent_id, f"agent:{agent.name}"))
                )
            else:
                await stack.enter_async_context(self.semaphore_for_agent(agent))

            # 5. Acquire route tag semaphore or serialize on the ancestor's single reentrant lane
            if ancestor_route_id is not None:
                await stack.enter_async_context(
                    self.semaphore_for_ancestor_lane((ancestor_route_id, f"route:{run.route_tag}"))
                )
            else:
                await stack.enter_async_context(self.semaphore_for_route_tag(run.route_tag))

            # 6. Acquire caller semaphore if not held by ancestor
            if not caller_exempt:
                await stack.enter_async_context(self.semaphore_for_caller(run.caller))

            # Register this run as an active holder on the execution stack
            with self._lock:
                self._active_run_holders[run.run_id] = {
                    "agent": agent.name,
                    "route_tag": run.route_tag,
                    "caller": run.caller,
                    "parent_run_id": parent_run_id,
                    "root_run_id": root_run_id,
                }

            try:
                if self.coordination.scope != "process":
                    return await self._run_with_distributed_limits(
                        run=run,
                        agent=agent,
                        call=call,
                        is_child=is_child,
                        root_run_id=root_run_id,
                        agent_held=ancestor_agent_id is not None,
                        route_tag_held=ancestor_route_id is not None,
                        caller_held=caller_exempt,
                    )
                return await self._run_admitted(
                    run=run,
                    agent=agent,
                    call=call,
                    is_root=not is_child,
                    root_run_id=root_run_id,
                )
            finally:
                with self._lock:
                    self._active_run_holders.pop(run.run_id, None)
                    lanes_to_remove = [k for k in self.ancestor_reentrant_semaphores if k[0] == run.run_id]
                    for k in lanes_to_remove:
                        self.ancestor_reentrant_semaphores.pop(k, None)

    async def _run_with_distributed_limits(
        self,
        *,
        run: AgentRun,
        agent: AgentSpec,
        call,
        is_child: bool = False,
        root_run_id: str = "",
        agent_held: bool = False,
        route_tag_held: bool = False,
        caller_held: bool = False,
    ) -> AgentRun:
        route_tag_limits = self.limits.route_tag_max_concurrency or {}
        caller_limits = self.limits.caller_max_concurrency or {}
        slots = []
        if not is_child:
            slots.append(("scheduler:global", self.limits.global_max_concurrency))
        else:
            slots.append((f"scheduler:nested_children:{root_run_id}", self.limits.max_active_children_per_root))
        if not agent_held:
            slots.append((f"scheduler:agent:{agent.name}", agent.max_concurrency))
        if not route_tag_held:
            slots.append((f"scheduler:route_tag:{run.route_tag}", route_tag_limits.get(run.route_tag, 10_000)))
        if not caller_held:
            slots.append((f"scheduler:caller:{run.caller}", caller_limits.get(run.caller, 10_000)))

        async with AsyncExitStack() as stack:
            for key, limit in slots:
                acquired = await stack.enter_async_context(
                    coordination_limited_slot(self.coordination, key, limit=max(1, int(limit)))
                )
                if not acquired:
                    raise RuntimeError(f"Scheduler coordination slot unavailable for {key}")
            return await self._run_admitted(
                run=run,
                agent=agent,
                call=call,
                is_root=not is_child,
                root_run_id=root_run_id,
            )

    async def _run_admitted(
        self,
        *,
        run: AgentRun,
        agent: AgentSpec,
        call,
        is_root: bool = True,
        root_run_id: str | None = None,
    ) -> AgentRun:
        with self._lock:
            self.running_by_agent[agent.name] += 1
            self.running_by_route_tag[run.route_tag] += 1
            self.running_by_caller[run.caller] += 1
        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="scheduler",
                event_type="run_dequeued",
                message="Task admitted by scheduler",
                data={"agent": agent.name, "route_tag": run.route_tag, "caller": run.caller},
            )
        )
        try:
            return await call()
        finally:
            with self._lock:
                self.running_by_agent[agent.name] -= 1
                self.running_by_route_tag[run.route_tag] -= 1
                self.running_by_caller[run.caller] -= 1
                if is_root and root_run_id:
                    self.nested_children_semaphores.pop(root_run_id, None)
                    lanes_to_remove = [k for k in self.ancestor_reentrant_semaphores if k[0] == root_run_id]
                    for k in lanes_to_remove:
                        self.ancestor_reentrant_semaphores.pop(k, None)

    def metrics(self) -> dict[str, object]:
        with self._lock:
            return {
                "global_available": self.global_semaphore._value,
                "coordination_backend": self.coordination.name,
                "coordination_scope": self.coordination.scope,
                "running_by_agent": dict(self.running_by_agent),
                "running_by_route_tag": dict(self.running_by_route_tag),
                "running_by_caller": dict(self.running_by_caller),
            }
