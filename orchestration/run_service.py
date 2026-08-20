"""Run submission use-case and transaction boundary."""

from __future__ import annotations

from domain import AgentRef, AgentRun, LogEvent, RunSubmission, utc_now
from domain.enums import CallbackStatus, ErrorType, RunStatus
from framework.registry import AgentRegistry, RegistryError
from framework.skill.errors import SkillError
from framework.skill.runtime import SkillRuntime
from infra.store import RunStore
from orchestration.conversation_service import ConversationService


class RunService:
    """Owns the run submission use-case."""

    def __init__(
        self,
        store: RunStore,
        agent_registry: AgentRegistry,
        skill_runtime: SkillRuntime | None = None,
    ) -> None:
        self.store = store
        self.agent_registry = agent_registry
        self.skill_runtime = skill_runtime
        self.conversations = ConversationService(store)

    def submit(self, submission: RunSubmission) -> tuple[AgentRun, bool]:
        """Return ``(run, is_new)``; ``is_new=False`` means an idempotent replay."""
        # 幂等：同一 (caller, route_tag, request_id) 重复提交，直接返回已有 run，不重复执行
        existing = self.store.runs.get_by_request_id(submission.caller, submission.route_tag, submission.request_id)
        if existing:
            self.store.logs.add(
                LogEvent(
                    run_id=existing.run_id,
                    trace_id=existing.trace_id,
                    component="run_api",
                    event_type="idempotent_replay",
                    message="Returned existing run for duplicate requestId",
                    data={"caller": submission.caller, "route_tag": submission.route_tag},
                )
            )
            return existing, False

        run = AgentRun(
            route_tag=submission.route_tag,
            caller=submission.caller,
            request_id=submission.request_id,
            input=submission.input,
            files=submission.files,
            callback=submission.callback,
            priority=submission.priority,
            timeout_seconds=submission.timeout_seconds,
            metadata=submission.metadata,
        )
        try:
            # 按 route_tag 确定性路由到具体 agent（rollout_key 支持灰度/分流），并校验入参
            agent = self.agent_registry.resolve(
                run.route_tag,
                version=submission.agent_version,
                caller=run.caller,
                metadata=run.metadata,
                rollout_key=run.request_id,
            )
            self.agent_registry.validate_input(agent, run.input)
            if agent.skills:
                if self.skill_runtime is None:
                    raise RegistryError("Skill runtime is not configured")
                run.skill_snapshots = self.skill_runtime.snapshots_for_agent(agent)
            # 路由成功 → 置 QUEUED 等待执行
            run.agent = AgentRef(name=agent.name, version=agent.version)
            run.status = RunStatus.QUEUED
            run.queue_time = utc_now()
            run.max_attempts = agent.retry_policy.max_attempts
            run.run_after = run.run_after or run.queue_time  # 可用于延迟执行
            # 有回调地址才需要投递，否则直接标 SKIPPED
            run.callback_status = (
                CallbackStatus.PENDING if run.callback and run.callback.url else CallbackStatus.SKIPPED
            )
        except (RegistryError, SkillError) as exc:
            # 路由/校验失败 → 直接落为 FAILED（不入队），错误类型区分"找不到 agent"与"参数非法"
            run.status = RunStatus.FAILED
            run.error_type = ErrorType.ROUTE_NOT_FOUND if "No enabled agent" in str(exc) else ErrorType.VALIDATION_ERROR
            run.error_message = str(exc)

        external_id = submission.external_id or submission.request_id
        run.update_time = utc_now()
        # 一个事务里同时建/取会话 + 建 run，保证两者要么都成功要么都回滚
        with self.store.unit_of_work() as uow:
            conversation = self.conversations.get_or_create(
                caller=submission.caller,
                external_id=external_id,
                route_tag=submission.route_tag,
                task_type=submission.task_type,
                source=submission.source,
                conversation_id=submission.conversation_id,
                conn=uow,
            )
            run.conversation_id = conversation.conversation_id
            self.store.runs.create(run, conn=uow)

        self.store.logs.add(
            LogEvent(
                run_id=run.run_id,
                trace_id=run.trace_id,
                component="run_api",
                event_type="run_submitted",
                message="Run submitted",
                data={
                    "route_tag": run.route_tag,
                    "status": run.status.value,
                    "conversation_id": run.conversation_id,
                    "agent": run.agent.model_dump() if run.agent else None,
                },
            )
        )
        return run, True
