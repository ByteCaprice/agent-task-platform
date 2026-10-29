"""Expose only the Run's assigned platform tools and Skills to Pi."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from framework.runtime.context import AgentContext
from framework.runtime.errors import ExternalSideEffectOutcomeUnknownError, RunCancelledError
from framework.runtime.pi.transport import PiProcessError
from framework.tool.errors import ToolExecutionError


class PiTools:
    def __init__(self, context: AgentContext, *, allow_scripts: bool = False) -> None:
        self.context = context
        self.platform_names = set(context.agent.tools)
        if len(self.platform_names) != len(context.agent.tools):
            raise ValueError("Pi tools must not contain duplicate names")
        self.definitions: list[dict[str, Any]] = []
        registry = getattr(context.tool_client, "registry", None)
        if self.platform_names and registry is None:
            raise ValueError("Pi tools require a ToolGateway with a registry")
        for name in context.agent.tools:
            spec = registry.get(name)
            self.definitions.append(
                {
                    "name": name,
                    "description": spec.description or f"Platform tool {name}",
                    "parameters": spec.input_schema or {"type": "object"},
                }
            )
        self.skill_names: set[str] = set()
        if context.skills.catalog():
            self._add_skill(
                "skill_load",
                "Load an assigned Skill before following its workflow.",
                {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["name", "reason"],
            )
            self._add_skill(
                "skill_read_resource",
                "Read a reference or asset from a loaded Skill.",
                {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                },
                ["name", "path"],
            )
            if allow_scripts:
                self._add_skill(
                    "skill_run_script",
                    "Run a declared script from a loaded Skill.",
                    {
                        "name": {"type": "string"},
                        "script": {"type": "string"},
                        "arguments": {"type": "object", "additionalProperties": True},
                    },
                    ["name", "script"],
                )

    def _add_skill(self, name: str, description: str, properties: dict, required: list[str]) -> None:
        if name in self.platform_names:
            raise ValueError(f"Platform tool {name!r} conflicts with a Skill bridge tool")
        self.skill_names.add(name)
        self.definitions.append(
            {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )

    async def call(self, name: str, arguments: Any) -> dict[str, Any]:
        """Normal tool failures are model-visible; cancellation/infrastructure failures are not."""
        if not isinstance(arguments, dict):
            return {"is_error": True, "text": "Tool arguments must be a JSON object"}
        try:
            if name in self.platform_names:
                result = await self.context.tool_client.call(
                    context=self.context,
                    tool_name=name,
                    input_data=arguments,
                )
            elif name in self.skill_names:
                result = await self._call_skill(name, arguments)
            else:
                raise ValueError(f"Tool {name!r} is not assigned to this Agent")
        except (RunCancelledError, ExternalSideEffectOutcomeUnknownError):
            # Do not weaken existing platform guarantees for accidentally assigned strict tools.
            raise
        except Exception as exc:
            if _infrastructure_failure(exc):
                # DB/audit errors must fail closed, not become a model-facing "recoverable" tool result.
                # SQLAlchemy exception text can contain statements, bind values and credentials.
                raise PiProcessError("Pi tool infrastructure failure") from None
            # Gateway/Skill audit owns diagnostics; don't forward arbitrary credential-bearing errors.
            return {"is_error": True, "text": f"Tool {name} failed ({type(exc).__name__})"}
        if isinstance(result, dict) and any(
            result.get(flag) is True for flag in ("_tool_error", "_guardrail_blocked", "_mcp_error")
        ):
            # These are explicitly model-facing gateway results, not arbitrary exception bodies.
            try:
                text = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise PiProcessError("Pi tool error result must contain finite JSON data") from None
            return {"is_error": True, "text": text}
        return {"is_error": False, "value": result}

    async def _call_skill(self, name: str, arguments: dict[str, Any]) -> Any:
        skill_name = _required(arguments, "name")
        assigned_name, separator, version = skill_name.partition("@")
        if separator and any(
            item.name == assigned_name and item.version == version for item in self.context.skills.catalog()
        ):
            skill_name = assigned_name
        if name == "skill_load":
            activation = await self.context.skills.activate(skill_name, reason=_required(arguments, "reason"))
            return activation.instructions
        if name == "skill_read_resource":
            path = _required(arguments, "path")
            text = await self.context.skills.read_text_resource(skill_name, path)
            return f"Untrusted Skill resource {skill_name}/{path}:\n{text}"
        arguments_data = arguments.get("arguments", {})
        if not isinstance(arguments_data, dict):
            raise ValueError("Skill script arguments must be an object")
        return await self.context.skills.run_script(skill_name, _required(arguments, "script"), arguments_data)


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _infrastructure_failure(error: BaseException) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (SQLAlchemyError, PiProcessError, MemoryError, RecursionError)):
            return True
        error = error.cause if isinstance(error, ToolExecutionError) else error.__cause__
    return False
