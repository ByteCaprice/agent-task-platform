"""Registry repositories — agent / tool config specs.

``ai_agent_config`` and ``ai_tool_config`` both use a surrogate ``id`` primary
key with a ``(name, version)`` UNIQUE constraint, so the common upsert / list
logic lives in ``_ConfigRepository`` and each concrete repository binds its
ORM entity and domain model type.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from domain import AgentSpec, SkillSpec, ToolSpec, utc_now
from infra.store.model import AiAgentConfig, AiSkillConfig, AiToolConfig
from infra.store.repository.base import BaseRepository


class _ConfigRepository(BaseRepository):
    _entity: Any
    _model: Any

    def save(self, spec: Any, *, conn: Any | None = None) -> Any:
        incoming = self._entity.from_domain(spec)
        incoming.last_time = utc_now()
        with self._write(conn) as s:
            self._upsert_by_key(s, incoming, ["name", "version"])
        return spec

    def delete(self, name: str, version: str, *, conn: Any | None = None) -> None:
        with self._write(conn) as s:
            s.execute(
                delete(self._entity).where(
                    self._entity.name == name,
                    self._entity.version == version,
                )
            )

    def list(self) -> list[Any]:
        with self._read() as s:
            entities = (
                s.execute(select(self._entity).order_by(self._entity.name.asc(), self._entity.version.asc()))
                .scalars()
                .all()
            )
            return [e.to_domain() for e in entities]


class AgentConfigRepository(_ConfigRepository):
    _entity = AiAgentConfig
    _model = AgentSpec


class ToolConfigRepository(_ConfigRepository):
    _entity = AiToolConfig
    _model = ToolSpec


class SkillConfigRepository(_ConfigRepository):
    _entity = AiSkillConfig
    _model = SkillSpec
