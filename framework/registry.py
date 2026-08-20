"""Plugin registries: ``AgentRegistry`` and ``ToolRegistry`` load agent/tool
specs from YAML, route requests to agents by route-tag, and validate call
input/output against each spec's JSON Schema.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import ValidationError, validate

from domain import AgentSkillRef, AgentSpec, SkillSpec, ToolSpec
from framework.skill.errors import SkillDisabledError, SkillNotFoundError


def _version_key(v: str) -> tuple[int, Any]:
    """Parse version string with packaging.version with natural fallback."""
    try:
        from packaging.version import parse

        return (1, parse(v))
    except Exception:
        natural = tuple((1, int(part)) if part.isdigit() else (0, part) for part in re.split(r"([0-9]+)", v) if part)
        return (0, natural)


class RegistryError(ValueError):
    pass


class AgentRegistry:
    def __init__(self, agents: list[AgentSpec]) -> None:
        self._agents: dict[tuple[str, str], AgentSpec] = {}
        self._routes: dict[str, AgentSpec] = {}
        for agent in agents:
            self.register(agent)

    @classmethod
    def from_file(cls, path: str | Path) -> AgentRegistry:
        payload = yaml.safe_load(Path(path).read_text()) or {}
        return cls([AgentSpec.model_validate(item) for item in payload.get("agents", [])])

    def register(self, agent: AgentSpec, *, make_default: bool = True) -> None:
        key = (agent.name, agent.version)
        previous = self._agents.get(key)
        if previous:
            for route_tag, routed in list(self._routes.items()):
                if routed.name == previous.name and routed.version == previous.version:
                    self._routes.pop(route_tag)
        self._agents[key] = agent
        if not agent.enabled or not make_default:
            if previous:
                for route_tag in previous.route_tags:
                    replacement = self._latest_enabled_for_tag(route_tag)
                    if replacement:
                        self._routes[route_tag] = replacement
            return
        for route_tag in agent.route_tags:
            existing = self._routes.get(route_tag)
            if existing and existing.name != agent.name:
                raise RegistryError(f"route_tag {route_tag!r} is already routed to {existing.name!r}")
            self._routes[route_tag] = agent

    def disable(self, name: str, version: str | None = None) -> list[AgentSpec]:
        changed: list[AgentSpec] = []
        for _key, agent in list(self._agents.items()):
            if agent.name != name or (version and agent.version != version):
                continue
            agent.enabled = False
            changed.append(agent)
            for route_tag, routed in list(self._routes.items()):
                if routed.name == agent.name and routed.version == agent.version:
                    self._routes.pop(route_tag)
                    replacement = self._latest_enabled_for_tag(route_tag)
                    if replacement:
                        self._routes[route_tag] = replacement
        if not changed:
            raise RegistryError(f"Unknown agent {name!r}")
        return changed

    def deregister(self, name: str, version: str | None = None) -> list[AgentSpec]:
        removed: list[AgentSpec] = []
        keys_to_remove = [
            key
            for key, agent in self._agents.items()
            if agent.name == name and (version is None or agent.version == version)
        ]
        for key in keys_to_remove:
            agent = self._agents.pop(key)
            removed.append(agent)
            for route_tag, routed in list(self._routes.items()):
                if routed.name == agent.name and routed.version == agent.version:
                    self._routes.pop(route_tag)
                    replacement = self._latest_enabled_for_tag(route_tag)
                    if replacement:
                        self._routes[route_tag] = replacement
        if not removed:
            raise RegistryError(f"Unknown agent {name!r}")
        return removed

    def set_default(self, name: str, version: str) -> AgentSpec:
        agent = self.get(name, version)
        if not agent.enabled:
            raise RegistryError(f"Agent {name!r}@{version} is disabled")
        for route_tag in agent.route_tags:
            existing = self._routes.get(route_tag)
            if existing and existing.name != agent.name:
                raise RegistryError(f"route_tag {route_tag!r} is already routed to {existing.name!r}")
            self._routes[route_tag] = agent
        return agent

    def resolve(
        self,
        route_tag: str,
        version: str | None = None,
        *,
        caller: str | None = None,
        metadata: dict[str, Any] | None = None,
        rollout_key: str | None = None,
    ) -> AgentSpec:
        if version:
            for agent in self._agents.values():
                if route_tag in agent.route_tags and agent.version == version and agent.enabled:
                    return agent
            raise RegistryError(f"No enabled agent version {version!r} for route_tag {route_tag!r}")
        agent = self._routes.get(route_tag)
        if not agent:
            raise RegistryError(f"No enabled agent for route_tag {route_tag!r}")
        return agent

    def get(self, name: str, version: str | None = None) -> AgentSpec:
        matches = [agent for (agent_name, _), agent in self._agents.items() if agent_name == name]
        if version:
            matches = [agent for agent in matches if agent.version == version]
        if not matches:
            raise RegistryError(f"Unknown agent {name!r}")
        enabled = [agent for agent in matches if agent.enabled]
        return max(enabled or matches, key=lambda item: _version_key(item.version))

    def list(self) -> list[AgentSpec]:
        return sorted(self._agents.values(), key=lambda item: (item.name, _version_key(item.version)))

    def get_optional(self, name: str, version: str) -> AgentSpec | None:
        return self._agents.get((name, version))

    def validate_input(self, agent: AgentSpec, data: dict[str, Any]) -> None:
        try:
            validate(instance=data, schema=agent.input_schema or {"type": "object"})
        except ValidationError as exc:
            raise RegistryError(f"Agent {agent.name!r} input validation failed: {exc.message}") from exc

    def validate_output(self, agent: AgentSpec, data: dict[str, Any]) -> None:
        try:
            validate(instance=data, schema=agent.output_schema or {"type": "object"})
        except ValidationError as exc:
            raise RegistryError(f"Agent {agent.name!r} output validation failed: {exc.message}") from exc

    def routes(self) -> dict[str, AgentSpec]:
        return dict(sorted(self._routes.items()))

    def _latest_enabled_for_tag(self, route_tag: str) -> AgentSpec | None:
        matches = [agent for agent in self._agents.values() if agent.enabled and route_tag in agent.route_tags]
        if not matches:
            return None
        return max(matches, key=lambda item: _version_key(item.version))


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @classmethod
    def from_file(cls, path: str | Path) -> ToolRegistry:
        payload = yaml.safe_load(Path(path).read_text()) or {}
        return cls([ToolSpec.model_validate(item) for item in payload.get("tools", [])])

    def get(self, name: str) -> ToolSpec:
        tool = self._tools.get(name)
        if not tool:
            raise RegistryError(f"Unknown tool {name!r}")
        if not tool.enabled:
            raise RegistryError(f"Tool {name!r} is disabled")
        return tool

    def register(self, tool: ToolSpec) -> ToolSpec:
        self._tools[tool.name] = tool
        return tool

    def disable(self, name: str) -> ToolSpec:
        tool = self._tools.get(name)
        if not tool:
            raise RegistryError(f"Unknown tool {name!r}")
        tool.enabled = False
        return tool

    def deregister(self, name: str) -> ToolSpec:
        tool = self._tools.pop(name, None)
        if not tool:
            raise RegistryError(f"Unknown tool {name!r}")
        return tool

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def get_optional(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def validate_allowed(self, tool: ToolSpec, agent_name: str) -> None:
        if tool.allowed_agents and agent_name not in tool.allowed_agents:
            raise RegistryError(f"Agent {agent_name!r} is not allowed to call tool {tool.name!r}")

    def validate_input(self, tool: ToolSpec, data: dict[str, Any]) -> None:
        try:
            validate(instance=data, schema=tool.input_schema or {"type": "object"})
        except ValidationError as exc:
            raise RegistryError(f"Tool {tool.name!r} input validation failed: {exc.message}") from exc

    def validate_output(self, tool: ToolSpec, data: dict[str, Any]) -> None:
        try:
            validate(instance=data, schema=tool.output_schema or {"type": "object"})
        except ValidationError as exc:
            raise RegistryError(f"Tool {tool.name!r} output validation failed: {exc.message}") from exc


class SkillRegistry:
    """Version-aware registry of deployed Skill artifacts."""

    def __init__(self, skills: list[SkillSpec]) -> None:
        self._skills: dict[tuple[str, str], SkillSpec] = {}
        self._defaults: dict[str, str] = {}
        for skill in skills:
            self.register(skill, make_default=False)
        for name in {s.name for s in skills}:
            self._select_default(name)

    def register(self, skill: SkillSpec, *, make_default: bool = True) -> SkillSpec:
        stored = skill.model_copy(deep=True)
        self._skills[(stored.name, stored.version)] = stored
        if not stored.enabled:
            if self._defaults.get(stored.name) == stored.version:
                self._defaults.pop(stored.name, None)
                self._select_default(stored.name)
        elif make_default:
            self._defaults[stored.name] = stored.version
        elif stored.name not in self._defaults:
            self._select_default(stored.name)
        return stored.model_copy(deep=True)

    def get(self, name: str, version: str | None = None) -> SkillSpec:
        resolved_version = version or self._defaults.get(name)
        if resolved_version is None:
            if any(skill_name == name for skill_name, _ in self._skills):
                raise SkillDisabledError(f"No enabled default version for Skill {name!r}")
            raise SkillNotFoundError(f"Unknown Skill {name!r}")
        skill = self._skills.get((name, resolved_version))
        if skill is None:
            raise SkillNotFoundError(f"Unknown Skill {name!r}@{resolved_version}")
        if not skill.enabled:
            raise SkillDisabledError(f"Skill {name!r}@{resolved_version} is disabled")
        return skill.model_copy(deep=True)

    def get_optional(self, name: str, version: str) -> SkillSpec | None:
        skill = self._skills.get((name, version))
        return skill.model_copy(deep=True) if skill else None

    def list(self) -> list[SkillSpec]:
        return [
            skill.model_copy(deep=True)
            for _, skill in sorted(self._skills.items(), key=lambda item: (item[0][0], _version_key(item[0][1])))
        ]

    def disable(self, name: str, version: str | None = None) -> list[SkillSpec]:
        changed: list[SkillSpec] = []
        for (skill_name, skill_version), skill in list(self._skills.items()):
            if skill_name != name or (version is not None and skill_version != version):
                continue
            disabled = skill.model_copy(update={"enabled": False})
            self._skills[(skill_name, skill_version)] = disabled
            changed.append(disabled.model_copy(deep=True))
        if not changed:
            raise SkillNotFoundError(f"Unknown Skill {name!r}")
        if self._defaults.get(name) in {item.version for item in changed}:
            self._defaults.pop(name, None)
            self._select_default(name)
        return changed

    def deregister(self, name: str, version: str | None = None) -> list[SkillSpec]:
        removed: list[SkillSpec] = []
        for key, skill in list(self._skills.items()):
            if skill.name != name or (version is not None and skill.version != version):
                continue
            self._skills.pop(key)
            removed.append(skill.model_copy(deep=True))
        if not removed:
            raise SkillNotFoundError(f"Unknown Skill {name!r}")
        if self._defaults.get(name) in {skill.version for skill in removed}:
            self._defaults.pop(name, None)
            self._select_default(name)
        return removed

    def resolve_for_agent(self, refs: list[AgentSkillRef]) -> list[SkillSpec]:
        seen: set[str] = set()
        resolved: list[SkillSpec] = []
        for ref in refs:
            if ref.name in seen:
                raise RegistryError(f"Agent declares Skill {ref.name!r} more than once")
            seen.add(ref.name)
            resolved.append(self.get(ref.name, ref.version))
        return resolved

    def _select_default(self, name: str) -> None:
        matches = [
            version for (skill_name, version), skill in self._skills.items() if skill_name == name and skill.enabled
        ]
        if matches:
            self._defaults[name] = max(matches, key=_version_key)
