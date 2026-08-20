"""Runtime contract: the ``Agent`` protocol every adapter implements, and the
``AgentContext`` dataclass that bundles the clients (tool, model, file, state)
handed to each agent's ``run`` call.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from domain import AgentSpec, RunSubmission
from domain.ids import utc_now
from framework.runtime.errors import RunCancelledError
from framework.runtime.state import AgentStateClient
from framework.skill.session import EmptySkillSession, SkillSession

if TYPE_CHECKING:
    from framework.runtime.stage_runner import StageRunner
    from framework.tool.stage_context import StageExecutionContext


class Agent(Protocol):
    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]: ...


def safe_stage_key(prefix: str, identifier: str) -> str:
    """Generate a sanitized, collision-resistant stage key and request ID.

    Handles Chinese characters, spaces, slashes, and arbitrarily long strings safely.
    """
    safe_prefix = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(prefix)).strip("_") or "stage"
    raw_str = str(identifier or "").strip()
    ident_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:16]
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", raw_str)[:24].strip("_")
    if cleaned:
        return f"{safe_prefix}:{cleaned}_{ident_hash}"
    return f"{safe_prefix}:{ident_hash}"


def _calculate_child_timeout(
    parent_context: AgentContext,
    child_spec: AgentSpec,
    explicit_timeout: float | None = None,
) -> float:
    """Calculate child timeout as min(parent_remaining_time, child_spec_timeout, explicit_timeout)."""
    candidates: list[float] = []

    # 1. Child spec timeout
    if child_spec.timeout_seconds and child_spec.timeout_seconds > 0:
        candidates.append(float(child_spec.timeout_seconds))

    # 2. Explicitly requested timeout
    if explicit_timeout is not None and explicit_timeout > 0:
        candidates.append(float(explicit_timeout))

    # 3. Parent remaining budget (if parent start time & timeout are known)
    parent_timeout = parent_context.agent.timeout_seconds
    if parent_timeout and parent_timeout > 0:
        start_time = parent_context.start_time
        if start_time is not None:
            elapsed = (utc_now() - start_time).total_seconds()
            remaining = max(1.0, parent_timeout - elapsed)
            candidates.append(remaining)
        else:
            candidates.append(float(parent_timeout))

    return min(candidates) if candidates else 300.0


@dataclass(slots=True)
class AgentContext:
    run_id: str
    route_tag: str
    trace_id: str
    metadata: dict[str, Any]
    files: list[Any]
    agent: AgentSpec
    tool_client: Any
    model_client: Any
    logger: Any
    file_client: Any
    state_client: AgentStateClient
    worker_id: str | None = None
    cancellation_signal: Any = None
    stage_runner: StageRunner | None = None
    skills: SkillSession | EmptySkillSession = field(default_factory=EmptySkillSession)
    runtime: Any = None
    agent_registry: Any = None
    start_time: datetime | None = None

    async def run_stage(
        self,
        stage_key: str,
        stage_input: Any,
        operation: Callable[[StageExecutionContext], Awaitable[Any]],
        *,
        definition_version: str = "1",
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Execute a durable stage, restoring its output after retry or recovery."""
        if self.skills.resolved_provenance() and "|skills:" not in definition_version:
            definition_version = f"{definition_version}|skills:{self.skills.snapshot_hash()}"
        if self.stage_runner is None:
            from framework.tool.stage_context import StageExecutionContext

            return await operation(
                StageExecutionContext.from_agent_context(
                    self,
                    stage_name=stage_key,
                    stage_input=stage_input,
                    definition_version=definition_version,
                    idempotency_key=idempotency_key or f"{self.run_id}:{stage_key}",
                )
            )
        return await self.stage_runner.run_stage(
            self,
            stage_key,
            stage_input,
            operation,
            definition_version=definition_version,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
        )

    async def run_child_agent(
        self,
        agent_name: str,
        input_data: dict[str, Any],
        *,
        stage_key: str | None = None,
        identifier: str | None = None,
        version: str | None = None,
        timeout_seconds: float | None = None,
        agent_spec: AgentSpec | None = None,
    ) -> dict[str, Any]:
        """Execute a specialist child run through RunManager/Scheduler/Executor.

        Features:
        - Resolves target AgentSpec from agent_registry
        - Uses safe hashing for stage key & child request ID
        - Bounds child timeout by min(parent_remaining, child_spec_timeout, timeout_seconds)
        - Submits child Run through formal RunManager with limits, retries, and unified state machine
        - Propagates parent cancellation to cancel child run
        - Executes as a durable stage in the parent workflow for idempotent replay
        """
        prefix = f"child-agent:{agent_name}"
        if stage_key is not None:
            effective_stage_key = safe_stage_key(stage_key, identifier or "") if identifier else stage_key
        elif identifier is not None:
            effective_stage_key = safe_stage_key(prefix, identifier)
        else:
            effective_stage_key = prefix

        # Upfront resolution of target AgentSpec to pin concrete version in durable stage
        target_spec = agent_spec
        if target_spec is None:
            registry = self.agent_registry or getattr(self.runtime, "agent_registry", None)
            if registry is not None:
                try:
                    if version is None:
                        target_spec = registry.get(agent_name)
                    elif hasattr(registry, "get_optional"):
                        target_spec = registry.get_optional(agent_name, version)
                    else:
                        target_spec = registry.get(agent_name, version)
                except Exception:
                    pass
            elif hasattr(self.runtime, "run_manager") and self.runtime.run_manager is not None:
                try:
                    reg = self.runtime.run_manager.agent_registry
                    if version is None:
                        target_spec = reg.get(agent_name)
                    elif hasattr(reg, "get_optional"):
                        target_spec = reg.get_optional(agent_name, version)
                    else:
                        target_spec = reg.get(agent_name, version)
                except Exception:
                    pass

        async def _execute_child_step(_stage_ctx: Any) -> dict[str, Any]:
            # 1. Resolve child AgentSpec if not already resolved
            nonlocal target_spec
            if target_spec is None:
                registry = self.agent_registry or getattr(self.runtime, "agent_registry", None)
                if registry is not None:
                    try:
                        target_spec = registry.get(agent_name, version)
                    except Exception as exc:
                        return {
                            "status": "FAILED",
                            "output": None,
                            "error_type": "AGENT_NOT_FOUND",
                            "error_message": str(exc),
                            "run_id": f"{self.run_id}:child:{effective_stage_key}",
                            "agent": {"name": agent_name, "version": version or "unknown"},
                        }
                elif hasattr(self.runtime, "run_manager") and self.runtime.run_manager is not None:
                    try:
                        target_spec = self.runtime.run_manager.agent_registry.get(agent_name, version)
                    except Exception as exc:
                        return {
                            "status": "FAILED",
                            "output": None,
                            "error_type": "AGENT_NOT_FOUND",
                            "error_message": str(exc),
                            "run_id": f"{self.run_id}:child:{effective_stage_key}",
                            "agent": {"name": agent_name, "version": version or "unknown"},
                        }

            if target_spec is None:
                return {
                    "status": "FAILED",
                    "output": None,
                    "error_type": "AGENT_NOT_FOUND",
                    "error_message": f"Agent {agent_name!r} (version={version!r}) is not registered",
                    "run_id": f"{self.run_id}:child:{effective_stage_key}",
                    "agent": {"name": agent_name, "version": version or "unknown"},
                }

            if not target_spec.enabled:
                return {
                    "status": "FAILED",
                    "output": None,
                    "error_type": "AGENT_DISABLED",
                    "error_message": f"Agent {target_spec.name}@{target_spec.version} is disabled",
                    "run_id": f"{self.run_id}:child:{effective_stage_key}",
                    "agent": {"name": target_spec.name, "version": target_spec.version},
                }

            # 2. Validate restricted input against child's input_schema
            if target_spec.input_schema:
                from jsonschema import ValidationError, validate

                try:
                    validate(instance=input_data, schema=target_spec.input_schema)
                except ValidationError as exc:
                    return {
                        "status": "FAILED",
                        "output": None,
                        "error_type": "VALIDATION_ERROR",
                        "error_message": f"Child agent input validation failed: {exc.message}",
                        "run_id": f"{self.run_id}:child:{effective_stage_key}",
                        "agent": {"name": target_spec.name, "version": target_spec.version},
                    }

            # 3. Calculate child timeout budget and hierarchical lineage
            child_timeout = _calculate_child_timeout(self, target_spec, timeout_seconds)
            child_route_tag = target_spec.route_tags[0] if target_spec.route_tags else self.route_tag
            child_req_id = safe_stage_key(f"child:{self.run_id}", effective_stage_key)
            parent_root_id = self.metadata.get("root_run_id") or self.run_id
            parent_depth = int(self.metadata.get("call_depth") or 0)
            child_depth = parent_depth + 1

            # 4. Route through formal RunManager
            run_manager = getattr(self.runtime, "run_manager", None)
            if run_manager is not None:
                submission = RunSubmission(
                    caller=f"agent:{self.agent.name}@{self.agent.version}",
                    route_tag=child_route_tag,
                    agent_version=target_spec.version,
                    request_id=child_req_id,
                    input=input_data,
                    timeout_seconds=int(child_timeout),
                    metadata={
                        **self.metadata,
                        "parent_run_id": self.run_id,
                        "root_run_id": parent_root_id,
                        "call_depth": child_depth,
                        "parent_stage_key": effective_stage_key,
                    },
                )
                child_run = await run_manager.submit(submission)
                try:
                    completed_run = await run_manager.run_now(child_run.run_id)
                except (asyncio.CancelledError, RunCancelledError):
                    run_manager.cancel(child_run.run_id)
                    raise
                except Exception as exc:
                    return {
                        "status": "FAILED",
                        "output": None,
                        "error_type": "EXECUTION_ERROR",
                        "error_message": str(exc),
                        "run_id": child_run.run_id,
                        "agent": {"name": target_spec.name, "version": target_spec.version},
                    }

                status_val = completed_run.status.value
                if status_val in {"SUCCEEDED", "AGENT_SUCCEEDED"}:
                    return {
                        "status": "SUCCEEDED",
                        "output": completed_run.output,
                        "error_type": None,
                        "error_message": None,
                        "run_id": completed_run.run_id,
                        "agent": {"name": target_spec.name, "version": target_spec.version},
                    }
                elif status_val == "TIMEOUT":
                    return {
                        "status": "TIMEOUT",
                        "output": None,
                        "error_type": "TIMEOUT",
                        "error_message": completed_run.error_message or f"Child agent timed out after {child_timeout}s",
                        "run_id": completed_run.run_id,
                        "agent": {"name": target_spec.name, "version": target_spec.version},
                    }
                else:
                    return {
                        "status": "FAILED",
                        "output": None,
                        "error_type": str(
                            completed_run.error_type.value if completed_run.error_type else "EXECUTION_ERROR"
                        ),
                        "error_message": completed_run.error_message or f"Child run finished with status {status_val}",
                        "run_id": completed_run.run_id,
                        "agent": {"name": target_spec.name, "version": target_spec.version},
                    }

            # 5. Explicit test stub runner for isolated unit tests
            test_stub = getattr(self, "_test_stub_runner", None) or getattr(self.runtime, "_test_stub_runner", None)
            if test_stub is not None:
                raw_out = await test_stub(target_spec, input_data)
                return {
                    "status": "SUCCEEDED",
                    "output": raw_out,
                    "error_type": None,
                    "error_message": None,
                    "run_id": f"{self.run_id}:child:{effective_stage_key}",
                    "agent": {"name": target_spec.name, "version": target_spec.version},
                }

            raise RuntimeError(
                "Child run execution requires RunManager to be configured on AgentRuntime. "
                "Direct un-governed execution is prohibited to ensure unified scheduling, concurrency limits, and state transitions."
            )

        # Run as a durable stage in the parent workflow with resolved child version
        resolved_child_version = target_spec.version if target_spec is not None else (version or "latest")
        child_def_version = f"child:{agent_name}@{resolved_child_version}"
        return await self.run_stage(
            effective_stage_key,
            input_data,
            _execute_child_step,
            definition_version=child_def_version,
        )
