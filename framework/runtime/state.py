"""Narrow state capabilities exposed to agent implementations and adapters."""

from __future__ import annotations

from typing import Any, Protocol

from domain import LogEvent, ModelCallRecord
from infra.store import RunStore


class AgentStateClient(Protocol):
    def add_log(self, event: LogEvent) -> LogEvent: ...

    def save_model_call(self, record: ModelCallRecord) -> ModelCallRecord: ...

    def get_prompt_spec(self, name: str, version: str | None = None) -> Any | None: ...


class RuntimeStateClient:
    """Adapter over RunStore that prevents agents from receiving the full store."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def add_log(self, event: LogEvent) -> LogEvent:
        return self._store.logs.add(event)

    def save_model_call(self, record: ModelCallRecord) -> ModelCallRecord:
        return self._store.model_calls.save(record)

    def get_prompt_spec(self, name: str, version: str | None = None) -> Any | None:
        lookup = getattr(self._store, "get_prompt_spec", None)
        if lookup is None:
            return None
        return lookup(name, version)
