"""Stage Execution Context — rich metadata for each pipeline step (inspired by OpenAI SDK's ToolContext).

Extends the run-level AgentContext with stage-specific tracking:
- stage_name, stage_index — which step in the pipeline
- stage_input — what data entered this stage
- timing — start_time, ended_at

Passed to every Agent.run() call so implementations can self-report their progress.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.runtime.context import AgentContext


@dataclass
class StageExecutionContext:
    """Rich context for a single stage/step in a run pipeline.

    Carries the full agent context plus stage-specific metadata, enabling
    stages to log progress, access shared dependencies, and know their
    identity in the pipeline.
    """

    # Task identity
    run_id: str
    trace_id: str
    route_tag: str
    caller: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # Agent identity
    agent_name: str = ""
    agent_version: str = ""

    # Stage identity
    stage_name: str = ""
    stage_index: int = 0
    stage_input: Any = None
    definition_version: str = "1"
    attempt: int = 1
    max_attempts: int = 1
    idempotency_key: str = ""
    checkpoint: dict[str, Any] | None = None

    # Timing
    start_time: str = ""

    # Parent context (access to tool_client, model_client, file_client, etc.)
    agent_context: Any = None
    _checkpoint_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )

    @classmethod
    def from_agent_context(
        cls,
        ctx: AgentContext,
        stage_name: str,
        stage_index: int = 0,
        stage_input: Any = None,
        definition_version: str = "1",
        attempt: int = 1,
        max_attempts: int = 1,
        idempotency_key: str = "",
        checkpoint: dict[str, Any] | None = None,
        checkpoint_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> StageExecutionContext:
        return cls(
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            route_tag=ctx.route_tag,
            caller=getattr(ctx.metadata, "get", lambda k: "")("caller") or "",
            metadata=ctx.metadata,
            agent_name=ctx.agent.name,
            agent_version=ctx.agent.version,
            stage_name=stage_name,
            stage_index=stage_index,
            stage_input=stage_input,
            definition_version=definition_version,
            attempt=attempt,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            checkpoint=checkpoint,
            start_time=datetime.now(UTC).isoformat(),
            agent_context=ctx,
            _checkpoint_writer=checkpoint_writer,
        )

    async def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Persist resumable state for the currently executing stage."""
        if self._checkpoint_writer is None:
            raise RuntimeError("Durable checkpoints are unavailable in this context")
        await self._checkpoint_writer(checkpoint)
        self.checkpoint = checkpoint

    def raise_if_cancelled(self) -> None:
        signal = getattr(self.agent_context, "cancellation_signal", None)
        if signal is not None and signal.is_set():
            from framework.runtime.errors import RunCancelledError

            raise RunCancelledError(f"Run {self.run_id!r} was canceled")
