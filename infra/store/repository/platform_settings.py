"""Repositories for the normalized platform configuration entities."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from domain import (
    ApiKeyConfig,
    utc_now,
)
from infra.store.model import (
    AiApiKeyConfig,
    AiCallbackConfig,
    AiModelConfig,
    AiRuntimeConfig,
)
from infra.store.repository.base import BaseRepository


class _NamedConfigRepository(BaseRepository):
    _entity: Any

    def save(self, config: Any, *, conn: Any | None = None) -> Any:
        incoming = self._entity.from_domain(config)
        incoming.last_time = utc_now()
        with self._write(conn) as session:
            self._upsert_by_key(session, incoming, ["name"])
        return config

    def get(self, name: str, *, conn: Any | None = None) -> Any | None:
        context = self._write(conn) if conn is not None else self._read()
        with context as session:
            entity = session.scalar(select(self._entity).where(self._entity.name == name))
            return entity.to_domain() if entity is not None else None

    def list(self, *, conn: Any | None = None) -> list[Any]:
        context = self._write(conn) if conn is not None else self._read()
        with context as session:
            entities = session.scalars(select(self._entity).order_by(self._entity.name)).all()
            return [entity.to_domain() for entity in entities]

    def delete(self, name: str, *, conn: Any | None = None) -> None:
        with self._write(conn) as session:
            session.execute(delete(self._entity).where(self._entity.name == name))


class RuntimeConfigRepository(_NamedConfigRepository):
    _entity = AiRuntimeConfig


class ModelConfigRepository(_NamedConfigRepository):
    _entity = AiModelConfig


class CallbackConfigRepository(_NamedConfigRepository):
    _entity = AiCallbackConfig


class ApiKeyConfigRepository(_NamedConfigRepository):
    _entity = AiApiKeyConfig

    def replace(self, configs: list[ApiKeyConfig], *, conn: Any | None = None) -> None:
        with self._write(conn) as session:
            session.execute(delete(AiApiKeyConfig))
            for config in configs:
                incoming = AiApiKeyConfig.from_domain(config)
                incoming.last_time = utc_now()
                session.add(incoming)
