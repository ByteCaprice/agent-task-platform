"""Compatibility shim — the schema is now defined by the ORM entities.

The single source of truth for the database schema is :mod:`store.model`
(declarative ORM entities).  This module re-exports the shared ``metadata``
and the per-table :class:`~sqlalchemy.Table` objects (``Entity.__table__``)
under their legacy names so that Alembic migrations, ``scripts/gen_sql.py``
and the cross-table maintenance helpers keep working unchanged.
"""

from __future__ import annotations

from infra.store.model import (
    AiAgentConfig,
    AiAgentRun,
    AiAgentStage,
    AiCallback,
    AiConversation,
    AiModelCall,
    AiRunLog,
    AiSkillConfig,
    AiToolConfig,
    metadata,
)

runs = AiAgentRun.__table__
stages = AiAgentStage.__table__
conversations = AiConversation.__table__
callbacks = AiCallback.__table__
log_events = AiRunLog.__table__
model_calls = AiModelCall.__table__
agent_configs = AiAgentConfig.__table__
tool_configs = AiToolConfig.__table__
skill_configs = AiSkillConfig.__table__

ALL_TABLES = [
    runs,
    stages,
    conversations,
    callbacks,
    log_events,
    model_calls,
    agent_configs,
    tool_configs,
    skill_configs,
]

__all__ = [
    "metadata",
    "runs",
    "stages",
    "conversations",
    "callbacks",
    "log_events",
    "model_calls",
    "agent_configs",
    "tool_configs",
    "skill_configs",
    "ALL_TABLES",
]
