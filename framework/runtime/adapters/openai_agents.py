"""``OpenAIAgentsSDKAgent``: adapter that runs an agent via the external
``openai-agents`` SDK, recording the model call, token usage, and cost to the
store.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

from domain import LogEvent, ModelCallRecord, utc_now
from framework.runtime.context import AgentContext
from framework.runtime.utils import estimate_cost, normalize_openai_base_url, summarize
from framework.skill.catalog import compose_instructions


class OpenAIAgentsSDKAgent:
    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        async def execute(_stage_context: Any) -> dict[str, Any]:
            return await self._run_once(context, input_data)

        return await context.run_stage(
            "openai-agents",
            input_data,
            execute,
            definition_version="1",
        )

    async def _run_once(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        try:
            agents_module = importlib.import_module("agents")
        except ImportError as exc:
            raise RuntimeError("openai-agents is not installed. Install with: pip install openai-agents") from exc

        sdk_agent_cls = agents_module.Agent
        runner = agents_module.Runner
        run_config_cls = agents_module.RunConfig
        multi_provider_cls = agents_module.MultiProvider
        prompt_name = self.runtime.get("prompt_name")
        prompt_spec = (
            context.state_client.get_prompt_spec(prompt_name, self.runtime.get("prompt_version"))
            if prompt_name
            else None
        )
        instructions = (
            prompt_spec.content
            if prompt_spec
            else self.runtime.get("instructions") or self.runtime.get("prompt") or context.agent.description
        )
        prompt_version = (
            prompt_spec.version if prompt_spec else self.runtime.get("prompt_version") or context.agent.version
        )
        base_instructions = str(instructions)
        catalog_max_chars = int(self.runtime.get("skill_catalog_max_chars", 8_000))
        composed = compose_instructions(
            base_instructions=base_instructions,
            session=context.skills,
            include_catalog=True,
            catalog_max_chars=catalog_max_chars,
        )
        prompt_hash = hashlib.sha256(composed.fingerprint_content.encode()).hexdigest()
        kwargs: dict[str, Any] = {
            "name": context.agent.name,
            "instructions": _dynamic_instructions(base_instructions, catalog_max_chars=catalog_max_chars),
        }
        if self.runtime.get("model"):
            kwargs["model"] = self.runtime["model"]
        sdk_tools = _build_platform_tools(
            agents_module,
            context,
            strict_json_schema=bool(self.runtime.get("strict_tool_schemas", False)),
        )
        if sdk_tools:
            kwargs["tools"] = sdk_tools
        skill_tools = _build_skill_tools(
            agents_module,
            context,
            strict_json_schema=bool(self.runtime.get("strict_tool_schemas", False)),
            allow_scripts=bool(
                self.runtime.get("allow_skill_scripts", False) or self.runtime.get("enable_skill_scripts", False)
            ),
        )
        if skill_tools:
            kwargs["tools"] = [*kwargs.get("tools", []), *skill_tools]
        if self.runtime.get("output_type"):
            kwargs["output_type"] = _load_target(self.runtime["output_type"])
        sdk_agent = sdk_agent_cls(**kwargs)
        sdk_input = self.runtime.get("input_template")
        if sdk_input:
            rendered_input = sdk_input.format(
                run_id=context.run_id,
                route_tag=context.route_tag,
                input=json.dumps(input_data, ensure_ascii=False),
                metadata=json.dumps(context.metadata, ensure_ascii=False),
            )
        else:
            rendered_input = json.dumps(
                {
                    "run_id": context.run_id,
                    "route_tag": context.route_tag,
                    "input": input_data,
                    "metadata": context.metadata,
                },
                ensure_ascii=False,
            )

        model_call = ModelCallRecord(
            run_id=context.run_id,
            trace_id=context.trace_id,
            agent_name=context.agent.name,
            agent_version=context.agent.version,
            model=self.runtime.get("model"),
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            input_summary=summarize(rendered_input),
            metadata={
                "runtime": "openai_agents",
                "prompt_name": prompt_name,
                "skill_catalog_hash": composed.catalog_hash,
            },
        )
        if composed.provenance:
            model_call.metadata.update(skills=composed.provenance, skill_catalog_hash=composed.catalog_hash)
        context.state_client.save_model_call(model_call)
        context.state_client.add_log(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component="model_gateway",
                event_type="openai_agents_run_started",
                message="OpenAI Agents SDK run started",
                data={
                    "call_id": model_call.call_id,
                    "agent": context.agent.name,
                    "model": self.runtime.get("model"),
                    "prompt_version": prompt_version,
                    "prompt_hash": prompt_hash,
                },
            )
        )

        model_defaults = getattr(context.model_client, "defaults", None) or {}
        base_url = normalize_openai_base_url(self.runtime.get("base_url") or model_defaults.get("base_url") or "")
        api_key = self.runtime.get("api_key") or model_defaults.get("api_key")
        model_name = self.runtime.get("model") or model_defaults.get("model")
        run_kwargs: dict[str, Any] = {}
        run_config: dict[str, Any] = {
            "workflow_name": self.runtime.get("workflow_name") or context.agent.name,
            "trace_id": _sdk_trace_id(context.trace_id),
            "group_id": context.run_id,
            "trace_metadata": {"platform_trace_id": context.trace_id, "run_id": context.run_id},
            "trace_include_sensitive_data": bool(self.runtime.get("trace_include_sensitive_data", False)),
        }
        if base_url and api_key:
            from openai import AsyncOpenAI

            openai_client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            provider = multi_provider_cls(openai_client=openai_client, openai_use_responses=False)
            run_config["model_provider"] = provider
            if model_name:
                run_config["model"] = _multi_provider_model_name(model_name)
        run_kwargs["run_config"] = run_config_cls(**run_config)
        if self.runtime.get("max_turns") is not None:
            run_kwargs["max_turns"] = self.runtime["max_turns"]
        try:
            result = await runner.run(sdk_agent, rendered_input, context=context, **run_kwargs)
            final_output = getattr(result, "final_output", result)
            if hasattr(final_output, "model_dump"):
                final_output = final_output.model_dump(mode="json")
            if isinstance(final_output, str):
                try:
                    parsed = json.loads(final_output)
                    if isinstance(parsed, dict):
                        final_output = parsed
                except Exception:
                    pass
            output = final_output if isinstance(final_output, dict) else {"data": final_output}
            output.setdefault("agent", {"name": context.agent.name, "version": context.agent.version})
            usage = extract_usage(result)
            model_call.status = "succeeded"
            model_call.prompt_tokens = usage["prompt_tokens"]
            model_call.completion_tokens = usage["completion_tokens"]
            model_call.total_tokens = usage["total_tokens"]
            model_call.estimated_cost = estimate_cost(
                model_call.total_tokens,
                float(self.runtime.get("cost_per_1k_tokens", 0.0)),
            )
            model_call.output_summary = summarize(output)
            model_call.finish_time = utc_now()
            final_composed = compose_instructions(
                base_instructions=base_instructions,
                session=context.skills,
                include_catalog=True,
                catalog_max_chars=int(self.runtime.get("skill_catalog_max_chars", 8_000)),
            )
            model_call.metadata.update(
                effective_skills=final_composed.provenance,
                final_prompt_hash=hashlib.sha256(final_composed.fingerprint_content.encode()).hexdigest(),
            )
            context.state_client.save_model_call(model_call)
            context.state_client.add_log(
                LogEvent(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    component="model_gateway",
                    event_type="openai_agents_run_succeeded",
                    message="OpenAI Agents SDK run succeeded",
                    data={
                        "call_id": model_call.call_id,
                        "agent": context.agent.name,
                        "usage": usage,
                        "estimated_cost": model_call.estimated_cost,
                    },
                )
            )
            return output
        except Exception as exc:
            model_call.status = "failed"
            model_call.error = f"{type(exc).__name__}: {exc}"
            model_call.finish_time = utc_now()
            context.state_client.save_model_call(model_call)
            raise


def extract_usage(result: Any) -> dict[str, int]:
    raw_responses = getattr(result, "raw_responses", None)
    if raw_responses:
        prompt = 0
        completion = 0
        total = 0
        for resp in raw_responses:
            usage = getattr(resp, "usage", None)
            if usage is None:
                continue
            prompt += int(getattr(usage, "input_tokens", 0) or 0)
            completion += int(getattr(usage, "output_tokens", 0) or 0)
            total += int(getattr(usage, "total_tokens", 0) or 0)
        if not total:
            total = prompt + completion
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    usage = getattr(result, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or prompt + completion)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    prompt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _build_platform_tools(
    agents_module: Any,
    context: AgentContext,
    *,
    strict_json_schema: bool,
) -> list[Any]:
    if not context.agent.tools:
        return []
    registry = getattr(context.tool_client, "registry", None)
    if registry is None:
        raise RuntimeError("OpenAI Agents SDK tools require a ToolGateway with a registry")
    function_tool_cls = agents_module.FunctionTool
    sdk_tools = []
    seen: set[str] = set()
    for tool_name in context.agent.tools:
        if tool_name in seen:
            raise ValueError(f"OpenAI Agents SDK tool {tool_name!r} is declared more than once")
        seen.add(tool_name)
        spec = registry.get(tool_name)

        async def invoke_tool(_sdk_context: Any, raw_input: str, *, _tool_name: str = tool_name) -> str:
            try:
                tool_input = json.loads(raw_input)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Tool {_tool_name!r} received invalid JSON arguments") from exc
            if not isinstance(tool_input, dict):
                raise ValueError(f"Tool {_tool_name!r} arguments must be a JSON object")
            output = await context.tool_client.call(
                context=context,
                tool_name=_tool_name,
                input_data=tool_input,
            )
            return json.dumps(output, ensure_ascii=False, sort_keys=True)

        sdk_tools.append(
            function_tool_cls(
                name=spec.name,
                description=spec.description or f"Platform tool {spec.name}",
                params_json_schema=spec.input_schema or {"type": "object"},
                on_invoke_tool=invoke_tool,
                strict_json_schema=strict_json_schema,
            )
        )
    return sdk_tools


def _dynamic_instructions(base_instructions: str, catalog_max_chars: int = 8_000):
    """Compose Skill-aware instructions for each SDK model turn."""

    def instructions(sdk_context: Any, _sdk_agent: Any) -> str:
        context = getattr(sdk_context, "context", None)
        if not isinstance(context, AgentContext):
            raise RuntimeError("OpenAI Agents SDK Skill instructions require an AgentContext")
        return compose_instructions(
            base_instructions=base_instructions,
            session=context.skills,
            include_catalog=True,
            catalog_max_chars=catalog_max_chars,
        ).instructions

    return instructions


def _build_skill_tools(
    agents_module: Any,
    context: AgentContext,
    *,
    strict_json_schema: bool,
    allow_scripts: bool = False,
) -> list[Any]:
    """Expose load/read bridges for assigned Skills. Scripts require explicit opt-in."""
    if not context.skills.catalog():
        return []
    function_tool_cls = agents_module.FunctionTool

    async def skill_load(sdk_context: Any, raw_input: str) -> str:
        payload = _tool_input(raw_input, "skill_load")
        skill_context = _sdk_agent_context(sdk_context, context)
        name = _assigned_skill_name(
            _required_string(payload, "name", "skill_load"),
            skill_context,
        )
        reason = _required_string(payload, "reason", "skill_load")
        activation = await skill_context.skills.activate(name, reason=reason)
        return activation.instructions

    async def skill_read_resource(sdk_context: Any, raw_input: str) -> str:
        payload = _tool_input(raw_input, "skill_read_resource")
        skill_context = _sdk_agent_context(sdk_context, context)
        name = _assigned_skill_name(
            _required_string(payload, "name", "skill_read_resource"),
            skill_context,
        )
        path = _required_string(payload, "path", "skill_read_resource")
        text = await skill_context.skills.read_text_resource(name, path)
        return f"Untrusted Skill resource {name}/{path}:\n{text}"

    tools = [
        function_tool_cls(
            name="skill_load",
            description="Load the full workflow for an assigned Skill. Use this before following a Skill workflow.",
            params_json_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
            on_invoke_tool=skill_load,
            strict_json_schema=strict_json_schema,
        ),
        function_tool_cls(
            name="skill_read_resource",
            description="Read a text resource from an already loaded Skill (e.g., path='references/guide.md' or 'references/high-risk-jurisdictions.md').",
            params_json_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                "required": ["name", "path"],
                "additionalProperties": False,
            },
            on_invoke_tool=skill_read_resource,
            strict_json_schema=strict_json_schema,
        ),
    ]

    if allow_scripts:

        async def skill_run_script(sdk_context: Any, raw_input: str) -> str:
            payload = _tool_input(raw_input, "skill_run_script")
            skill_context = _sdk_agent_context(sdk_context, context)
            name = _assigned_skill_name(
                _required_string(payload, "name", "skill_run_script"),
                skill_context,
            )
            script = _required_string(payload, "script", "skill_run_script")
            arguments = payload.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = {"input": arguments}
            elif not isinstance(arguments, dict):
                arguments = {"input": arguments}
            result = await skill_context.skills.run_script(name, script, arguments)
            return json.dumps(result, ensure_ascii=False)

        tools.append(
            function_tool_cls(
                name="skill_run_script",
                description="Run a declared script from an already loaded Skill (e.g. script='calculate_score' or 'scripts/calc.py'). Pass parameters as a JSON object in 'arguments'.",
                params_json_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "script": {"type": "string"},
                        "arguments": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["name", "script"],
                    "additionalProperties": False,
                },
                on_invoke_tool=skill_run_script,
                strict_json_schema=strict_json_schema,
            )
        )

    return tools


def _sdk_agent_context(sdk_context: Any, fallback: AgentContext) -> AgentContext:
    sdk_agent_context = getattr(sdk_context, "context", fallback)
    if not isinstance(sdk_agent_context, AgentContext):
        raise RuntimeError("OpenAI Agents SDK Skill tools require an AgentContext")
    return sdk_agent_context


def _tool_input(raw_input: str, tool_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{tool_name} received invalid JSON arguments") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{tool_name} arguments must be a JSON object")
    return payload


def _required_string(payload: dict[str, Any], key: str, tool_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{tool_name} requires a non-empty string {key!r}")
    return value


def _assigned_skill_name(value: str, context: AgentContext) -> str:
    """Accept catalog-style ``name@version`` only for this Run's assigned Skill."""
    name, separator, version = value.partition("@")
    if not separator:
        return value
    if any(item.name == name and item.version == version for item in context.skills.catalog()):
        return name
    return value


def _load_target(target: Any) -> Any:
    if not isinstance(target, str):
        return target
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("output_type must use 'module:attribute' syntax")
    return getattr(importlib.import_module(module_name), attribute)


def _sdk_trace_id(platform_trace_id: str) -> str:
    """Map the platform trace identity to the SDK's required trace_<32 hex> form."""
    return f"trace_{hashlib.sha256(platform_trace_id.encode()).hexdigest()[:32]}"


def _multi_provider_model_name(model_name: str) -> str:
    """Route a custom OpenAI-compatible client through SDK MultiProvider."""
    return model_name if model_name.startswith("openai/") else f"openai/{model_name}"
