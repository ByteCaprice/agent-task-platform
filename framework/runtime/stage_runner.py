"""Durable stage execution and resume support for existing Agent implementations."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from domain import AgentRun, AgentSpec, AgentStage, LogEvent
from domain.enums import RunStatus, StageStatus
from framework.runtime.errors import (
    ExternalSideEffectOutcomeUnknownError,
    RunCancelledError,
    StageAttemptsExhaustedError,
    StageStateError,
    StaleStageExecutionError,
)
from framework.tool.stage_context import StageExecutionContext

if TYPE_CHECKING:
    from framework.runtime.context import AgentContext
    from infra.store import RunStore


StageOperation = Callable[[StageExecutionContext], Awaitable[Any]]
_STAGE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class StageRunner:
    """Executes one logical stage or returns its previously persisted output."""

    def __init__(
        self,
        *,
        store: RunStore,
        run: AgentRun,
        agent: AgentSpec,
        cancellation_signal: Any = None,
        hooks: Any = None,
    ) -> None:
        self.store = store
        self.run = run
        self.agent = agent
        self.cancellation_signal = cancellation_signal
        self.hooks = hooks
        self._seen: set[str] = set()
        self._next_index = 0

    async def run_stage(
        self,
        agent_context: AgentContext,
        stage_key: str,
        stage_input: Any,
        operation: StageOperation,
        *,
        definition_version: str = "1",
        max_attempts: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self._validate_key(stage_key)
        if stage_key in self._seen:
            raise StageStateError(f"Stage state validation failed: duplicate stage key {stage_key!r}")
        self._seen.add(stage_key)
        stage_index = self._next_index
        self._next_index += 1
        self._raise_if_cancelled()

        input_hash = _stable_hash(stage_input)
        stage = self.store.stages.get_or_create(
            AgentStage(
                run_id=self.run.run_id,
                trace_id=self.run.trace_id,
                agent_name=self.agent.name,
                agent_version=self.agent.version,
                stage_key=stage_key,
                stage_index=stage_index,
                definition_version=definition_version,
                max_attempts=max(1, max_attempts or self.agent.retry_policy.max_attempts),
                idempotency_key=idempotency_key or f"{self.run.run_id}:{stage_key}",
                input_hash=input_hash,
            )
        )
        self._validate_state(stage, stage_index, definition_version, input_hash)

        if stage.status == StageStatus.SUCCEEDED:
            self._log(
                "stage_resumed",
                f"Stage {stage_key} restored from durable state",
                stage_key=stage_key,
                stage_index=stage_index,
                attempts=stage.attempts,
            )
            return copy.deepcopy(stage.output)
        if stage.status == StageStatus.CANCELED:
            raise RunCancelledError(f"Stage {stage_key!r} was canceled")
        if stage.status == StageStatus.OUTCOME_UNKNOWN:
            raise ExternalSideEffectOutcomeUnknownError(
                stage.error_message or f"Stage {stage_key!r} external outcome is unknown"
            )

        execution_id = uuid4().hex
        active = self.store.stages.begin_attempt(
            self.run.run_id,
            stage_key,
            run_attempt=self.run.attempts,
            execution_id=execution_id,
        )
        if active is None:
            raise StageAttemptsExhaustedError(f"Stage {stage_key!r} exhausted its configured attempts")
        self._set_current_step(stage_key)
        stage_context = StageExecutionContext.from_agent_context(
            agent_context,
            stage_name=stage_key,
            stage_index=stage_index,
            stage_input=stage_input,
            definition_version=definition_version,
            attempt=active.attempts,
            max_attempts=active.max_attempts,
            idempotency_key=active.idempotency_key,
            checkpoint=copy.deepcopy(active.checkpoint),
            checkpoint_writer=lambda value: self._save_checkpoint(
                stage_key,
                execution_id,
                value,
            ),
        )
        self._log(
            "stage_started",
            f"Stage {stage_key} started",
            stage_key=stage_key,
            stage_index=stage_index,
            attempt=active.attempts,
            idempotency_key=active.idempotency_key,
        )
        await self._hook_start(stage_context)
        try:
            output = _json_value(await operation(stage_context), label="stage output")
            self._raise_if_cancelled()
            if not self.store.stages.mark_succeeded(
                self.run.run_id,
                stage_key,
                execution_id=execution_id,
                output=output,
            ):
                persisted = self.store.stages.get(self.run.run_id, stage_key)
                if persisted is not None and persisted.status == StageStatus.OUTCOME_UNKNOWN:
                    raise ExternalSideEffectOutcomeUnknownError(
                        persisted.error_message or f"Stage {stage_key!r} external outcome is unknown"
                    )
                raise StaleStageExecutionError(f"Stage {stage_key!r} result belongs to a superseded execution")
        except RunCancelledError as exc:
            self.store.stages.mark_canceled(
                self.run.run_id,
                stage_key,
                execution_id=execution_id,
            )
            await self._hook_end(stage_context, error=exc)
            raise
        except asyncio.CancelledError as exc:
            # Keep the stage RUNNING with its last checkpoint. A later worker can
            # claim it with a new execution token after timeout or process loss.
            self._log(
                "stage_interrupted",
                f"Stage {stage_key} execution was interrupted",
                stage_key=stage_key,
                stage_index=stage_index,
                attempt=active.attempts,
            )
            await self._hook_end(stage_context, error=exc)
            raise
        except StaleStageExecutionError as exc:
            self._log(
                "stage_superseded",
                f"Stage {stage_key} execution was superseded",
                stage_key=stage_key,
                stage_index=stage_index,
                attempt=active.attempts,
            )
            await self._hook_end(stage_context, error=exc)
            raise
        except ExternalSideEffectOutcomeUnknownError as exc:
            self.store.stages.mark_outcome_unknown(
                self.run.run_id,
                stage_key,
                execution_id=execution_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self._log(
                "stage_outcome_unknown",
                f"Stage {stage_key} external outcome is unknown",
                stage_key=stage_key,
                stage_index=stage_index,
                attempt=active.attempts,
            )
            await self._hook_end(stage_context, error=exc)
            raise
        except Exception as exc:
            self.store.stages.mark_failed(
                self.run.run_id,
                stage_key,
                execution_id=execution_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self._log(
                "stage_failed",
                f"Stage {stage_key} failed",
                stage_key=stage_key,
                stage_index=stage_index,
                attempt=active.attempts,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            await self._hook_end(stage_context, error=exc)
            raise
        self._log(
            "stage_succeeded",
            f"Stage {stage_key} succeeded",
            stage_key=stage_key,
            stage_index=stage_index,
            attempt=active.attempts,
            output_type=type(output).__name__,
        )
        await self._hook_end(stage_context, output=output)
        return copy.deepcopy(output)

    async def _save_checkpoint(
        self,
        stage_key: str,
        execution_id: str,
        checkpoint: dict[str, Any],
    ) -> None:
        self._raise_if_cancelled()
        value = _json_value(checkpoint, label="stage checkpoint")
        if not isinstance(value, dict):
            raise TypeError("stage checkpoint must be a JSON object")
        if not self.store.stages.save_checkpoint(
            self.run.run_id,
            stage_key,
            execution_id=execution_id,
            checkpoint=value,
        ):
            raise StaleStageExecutionError(f"Stage {stage_key!r} checkpoint belongs to a superseded execution")
        self._log(
            "stage_checkpointed",
            f"Stage {stage_key} checkpoint saved",
            stage_key=stage_key,
            checkpoint_keys=sorted(value),
        )

    def _validate_state(
        self,
        stage: AgentStage,
        stage_index: int,
        definition_version: str,
        input_hash: str,
    ) -> None:
        if stage.schema_version != "1":
            raise StageStateError(f"Stage state validation failed: unsupported schema version {stage.schema_version!r}")
        if stage.agent_name != self.agent.name or stage.agent_version != self.agent.version:
            raise StageStateError("Stage state validation failed: agent identity changed")
        if stage.stage_index != stage_index:
            raise StageStateError(
                f"Stage state validation failed: {stage.stage_key!r} moved from index "
                f"{stage.stage_index} to {stage_index}"
            )
        if stage.definition_version != definition_version:
            raise StageStateError(f"Stage state validation failed: {stage.stage_key!r} definition changed")
        if stage.input_hash != input_hash:
            raise StageStateError(f"Stage state validation failed: {stage.stage_key!r} input changed")

    def _raise_if_cancelled(self) -> None:
        run = self.store.runs.get(self.run.run_id)
        if (self.cancellation_signal is not None and self.cancellation_signal.is_set()) or (
            run is not None and run.status == RunStatus.CANCELED
        ):
            raise RunCancelledError(f"Run {self.run.run_id!r} was canceled")

    def _set_current_step(self, stage_key: str) -> None:
        run = self.store.runs.get(self.run.run_id)
        if run is None or run.status == RunStatus.CANCELED:
            self._raise_if_cancelled()
            return
        run.current_step = f"stage:{stage_key}"
        if not self.store.runs.update_if_current(
            run,
            expected_statuses={run.status},
            expected_worker=self.run.worker,
            match_worker=True,
        ):
            raise RunCancelledError(f"Run {self.run.run_id!r} was canceled or is owned by another worker")

    def _log(self, event_type: str, message: str, **data: Any) -> None:
        self.store.logs.add(
            LogEvent(
                run_id=self.run.run_id,
                trace_id=self.run.trace_id,
                component="stage_runner",
                event_type=event_type,
                message=message,
                data=data,
            )
        )

    async def _hook_start(self, context: StageExecutionContext) -> None:
        callback = getattr(self.hooks, "on_stage_start", None)
        if callback is not None:
            try:
                await callback(context)
            except Exception as exc:
                self._log_hook_error("on_stage_start", exc)

    async def _hook_end(
        self,
        context: StageExecutionContext,
        *,
        output: Any = None,
        error: BaseException | None = None,
    ) -> None:
        callback = getattr(self.hooks, "on_stage_end", None)
        if callback is not None:
            try:
                await callback(context, output=output, error=error)
            except Exception as exc:
                self._log_hook_error("on_stage_end", exc)

    def _log_hook_error(self, hook: str, error: Exception) -> None:
        self._log(
            "lifecycle_hook_failed",
            f"Lifecycle hook {hook} failed",
            hook=hook,
            error_type=type(error).__name__,
        )

    @staticmethod
    def _validate_key(stage_key: str) -> None:
        if not _STAGE_KEY.fullmatch(stage_key):
            raise ValueError("stage_key must be 1-128 characters using letters, numbers, '.', '_', ':', or '-'")


def _json_value(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON serializable") from exc


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value, label="stage input"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
