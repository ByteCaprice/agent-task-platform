"""API key management with scope-based access control.

Supports two configuration formats:

1. Simple list of strings (backward compatible):
   AUTH__API_KEYS=["key1", "key2"]

2. List of objects with metadata:
   AUTH__API_KEYS=[
      {"key": "key1", "name": "service-client", "scopes": ["runs", "operations"]},
     {"key": "key2", "name": "admin-cli", "scopes": ["*"]}
   ]

Scopes:
  - ``runs`` — submit/query/cancel runs
  - ``admin`` — agent/tool registry management, release requests, maintenance
  - ``operations`` — queue inspection, alerts, audit events, metrics
  - ``*`` — all scopes (superuser)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApiKeyEntry:
    """Metadata for a single API key."""

    key: str
    name: str = ""
    scopes: set[str] = field(default_factory=lambda: {"*"})
    enabled: bool = True

    def has_scope(self, required_scope: str) -> bool:
        if not self.enabled:
            return False
        if "*" in self.scopes:
            return True
        return required_scope in self.scopes


class ApiKeyRegistry:
    """Registry of API keys with scope-based access control."""

    def __init__(self, raw_config: Any = None) -> None:
        self._entries: dict[str, ApiKeyEntry] = {}
        self._load(raw_config)

    def _load(self, raw_config: Any) -> None:
        if not raw_config:
            return
        if isinstance(raw_config, str):
            raw_config = [raw_config]
        if not isinstance(raw_config, list):
            return

        for item in raw_config:
            if isinstance(item, str):
                entry = ApiKeyEntry(key=item, name=item[:8], scopes={"*"})
                self._entries[item] = entry
            elif isinstance(item, dict):
                key = item.get("key", "")
                if not key:
                    continue
                scopes_raw = item.get("scopes", ["*"])
                if isinstance(scopes_raw, str):
                    scopes = {s.strip() for s in scopes_raw.split(",") if s.strip()}
                else:
                    scopes = set(scopes_raw)
                entry = ApiKeyEntry(
                    key=key,
                    name=item.get("name", key[:8]),
                    scopes=scopes or {"*"},
                    enabled=item.get("enabled", True),
                )
                self._entries[key] = entry

    def authenticate(self, api_key: str | None) -> ApiKeyEntry | None:
        """Return the matching :class:`ApiKeyEntry` or *None*."""
        if not api_key:
            return None
        entry = self._entries.get(api_key)
        if entry and entry.enabled:
            return entry
        return None

    def authorize(self, api_key: str | None, required_scope: str) -> ApiKeyEntry | None:
        """Return entry if key is valid and has the required scope."""
        entry = self.authenticate(api_key)
        if entry and entry.has_scope(required_scope):
            return entry
        return None

    @property
    def keys(self) -> set[str]:
        """Return the set of all valid API key strings (for backward compat)."""
        return {k for k, v in self._entries.items() if v.enabled}

    @property
    def is_configured(self) -> bool:
        """Return True if any API keys are registered."""
        return bool(self._entries)

    def list_entries(self) -> list[dict[str, Any]]:
        """Return metadata for all keys (without the key itself)."""
        return [{"name": v.name, "scopes": sorted(v.scopes), "enabled": v.enabled} for v in self._entries.values()]
