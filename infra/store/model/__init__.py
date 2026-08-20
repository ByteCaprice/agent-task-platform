"""SQLAlchemy ORM entities — one class per table.

Entity == table.  One domain (Pydantic) field maps to exactly one column;
``dict`` / ``list`` / nested-model fields are stored in their own JSONB column.
Each entity carries ``from_domain`` / ``to_domain`` so the field mapping lives
next to the table definition and repositories stay thin.
"""

from infra.store.model.ai_agent_config import AiAgentConfig
from infra.store.model.ai_agent_run import AiAgentRun
from infra.store.model.ai_agent_stage import AiAgentStage
from infra.store.model.ai_callback import AiCallback
from infra.store.model.ai_conversation import AiConversation
from infra.store.model.ai_model_call import AiModelCall
from infra.store.model.ai_run_log import AiRunLog
from infra.store.model.ai_skill_config import AiSkillConfig
from infra.store.model.ai_tool_config import AiToolConfig
from infra.store.model.base import AUTO_PK, JSON_VARIANT, Base

metadata = Base.metadata

__all__ = [
    "Base",
    "metadata",
    "AUTO_PK",
    "JSON_VARIANT",
    "AiAgentConfig",
    "AiCallback",
    "AiConversation",
    "AiModelCall",
    "AiRunLog",
    "AiAgentRun",
    "AiAgentStage",
    "AiToolConfig",
    "AiSkillConfig",
]
