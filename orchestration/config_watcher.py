"""ConfigWatcher: polls the DB registry tables (``ai_agent_config`` /
``ai_tool_config``) for agent/tool changes and hot-reloads them into the
in-memory registries at runtime.

The DB is the source of truth (YAML is only a startup seed; see
``server._load_agent_registry``).  Any writer of the DB — the admin API, a DBA
running per-env seed SQL, or another node — is picked up here.  Settings
(``.env`` / ``settings.yaml``) remain cold config: a change is logged as a
warning but not applied, because runtime objects are built at app startup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from domain import LogEvent, new_trace_id
from framework.registry import AgentRegistry, SkillRegistry, ToolRegistry
from framework.skill.loader import SkillLoader
from infra.store import RunStore


class ConfigWatcher:
    def __init__(
        self,
        *,
        config_dir: str | Path,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry | None = None,
        skill_loader: SkillLoader | None = None,
        store: RunStore,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.skill_loader = skill_loader
        self.store = store
        self.poll_interval_seconds = poll_interval_seconds
        self._mtimes: dict[str, float] = {}
        self._running = False
        self.last_error: str | None = None
        self.last_reload_counts: dict[str, int] = {}

    def _snapshot_mtimes(self) -> dict[str, float]:
        mtimes: dict[str, float] = {}
        for filename in [".env", "settings.yaml"]:
            path = self.config_dir / filename
            if path.exists():
                mtimes[filename] = path.stat().st_mtime
        return mtimes

    def _reload_agents(self) -> int:
        """Sync the in-memory agent registry to ``ai_agent_config`` (DB truth).

        Removals are applied before upserts so a rename (old row deleted, new
        row added) frees its ``route_tag`` before the new agent claims it.
        """
        db_specs = {(s.name, s.version): s for s in self.store.agents.list()}
        reg_specs = {(s.name, s.version): s for s in self.agent_registry.list()}
        count = 0
        for key, spec in reg_specs.items():
            if key not in db_specs:
                self.agent_registry.deregister(spec.name, spec.version)
                count += 1
        for key, spec in db_specs.items():
            existing = reg_specs.get(key)
            if existing is not None and existing.model_dump(mode="json") == spec.model_dump(mode="json"):
                continue
            self.agent_registry.register(spec, make_default=True)
            count += 1
        return count

    def _reload_tools(self) -> int:
        """Sync the in-memory tool registry to ``ai_tool_config`` (DB truth).

        The registry is keyed by tool name; ``store.tools.list()`` is ordered by
        ``(name, version)`` so the highest version wins per name, matching the
        startup loader.
        """
        db_specs = {s.name: s for s in self.store.tools.list()}
        reg_specs = {s.name: s for s in self.tool_registry.list()}
        count = 0
        for name in reg_specs:
            if name not in db_specs:
                self.tool_registry.deregister(name)
                count += 1
        for name, spec in db_specs.items():
            existing = reg_specs.get(name)
            if existing is not None and existing.model_dump(mode="json") == spec.model_dump(mode="json"):
                continue
            self.tool_registry.register(spec)
            count += 1
        return count

    def _reload_skills(self) -> int:
        """Sync verified DB Skill artifacts without mutating existing Sessions."""
        if self.skill_registry is None or self.skill_loader is None:
            return 0
        db_specs = {(spec.name, spec.version): spec for spec in self.store.skills.list()}
        for spec in db_specs.values():
            self.skill_loader.verify(spec)
        reg_specs = {(spec.name, spec.version): spec for spec in self.skill_registry.list()}
        count = 0
        for name, version in reg_specs.keys() - db_specs.keys():
            self.skill_registry.deregister(name, version)
            count += 1
        for key, spec in db_specs.items():
            existing = reg_specs.get(key)
            if existing is not None and existing.model_dump(mode="json") == spec.model_dump(mode="json"):
                continue
            self.skill_registry.register(
                spec, make_default=key[0] not in {item.name for item in self.skill_registry.list()}
            )
            count += 1
        return count

    def _reload_settings(self) -> int:
        """Settings are cold config because runtime objects are built at app startup."""
        path = self.config_dir / ".env"
        if not path.exists():
            path = self.config_dir / "settings.yaml"
        if not path.exists():
            return 0
        self.store.logs.add(
            LogEvent(
                trace_id=new_trace_id(),
                component="config_watcher",
                event_type="settings_reload_skipped",
                level="WARNING",
                message=f"{path.name} is cold config; restart the server for settings changes to take effect",
                data={"file": str(path)},
            )
        )
        return 0

    async def run_forever(self) -> None:
        self._mtimes = self._snapshot_mtimes()
        self._running = True
        while self._running:
            await asyncio.sleep(self.poll_interval_seconds)
            try:
                # DB-truth: poll the registry tables every tick (cheap no-op
                # when nothing changed — diffed by content before re-register).
                self._reload_agents()
                self._reload_tools()
                self._reload_skills()
                # Settings files are cold config: only warn on change.
                current = self._snapshot_mtimes()
                for filename, mtime in current.items():
                    if mtime != self._mtimes.get(filename):
                        self._reload_settings()
                self._mtimes = current
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.store.logs.add(
                    LogEvent(
                        trace_id=new_trace_id(),
                        component="config_watcher",
                        event_type="config_reload_failed",
                        level="ERROR",
                        message="Config hot reload failed",
                        data={"error": self.last_error},
                    )
                )

    def stop(self) -> None:
        self._running = False

    def reload_all(self) -> dict[str, int]:
        self.last_error = None
        self.last_reload_counts = {
            "agents": self._reload_agents(),
            "tools": self._reload_tools(),
            "skills": self._reload_skills(),
            "settings": self._reload_settings(),
        }
        return dict(self.last_reload_counts)
