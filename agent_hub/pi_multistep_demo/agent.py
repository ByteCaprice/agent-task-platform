"""Build isolated Pi registrations; the model, not Python, chooses and executes each step."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain import AgentSpec, ToolSpec


class DemoResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    delivery_method: Literal["standard"]
    currency: Literal["CNY"]
    total_cents: Literal[3980]
    verified: Literal[True]
    quote_token: str = Field(pattern=r"^[a-f0-9]{24}$")


def build_demo_specs(tag: str, model_name: str) -> tuple[list[AgentSpec], list[ToolSpec]]:
    """Create a fresh reserved namespace, without credentials or global configuration changes."""
    if not re.fullmatch(r"[a-f0-9]{12}", tag):
        raise ValueError("Demo tag must be 12 lowercase hexadecimal characters")
    agent_name = f"pi-multistep-demo-{tag}"
    names = {step: f"pi-demo-{step}-{tag}" for step in ("load-order", "quote-order", "verify-quote")}
    prompt = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
    for step, name in names.items():
        prompt = prompt.replace(f"{{{{{step}}}}}", name)
    tool_specs = []
    # The runtime loads these endpoints through ToolGateway, never by importing them into the model worker.
    from plugins.tools import pi_demo

    for step, target in (
        ("load-order", "load_order"),
        ("quote-order", "quote_order"),
        ("verify-quote", "verify_quote"),
    ):
        func = getattr(pi_demo, target)
        schema = func.__tool_schema__
        tool_specs.append(
            ToolSpec(
                name=names[step],
                description=schema.description or "Synthetic demo tool",
                input_schema=schema.params_json_schema,
                output_schema={"type": "object"},
                endpoint={"protocol": "python", "target": f"plugins.tools.pi_demo:{target}"},
                operation_type="read_only",
                timeout_seconds=5,
                max_concurrency=2,
                retry_policy={"max_attempts": 1, "backoff_seconds": 0},
                allowed_agents=[agent_name],
                owner="pi-demo",
                managed_by="pi-demo",
                enabled=False,
            )
        )
    spec = AgentSpec(
        name=agent_name,
        version="1.0.0",
        route_tags=[f"demo.pi.multistep.{tag}"],
        description="Synthetic Pi multi-step order demo, not a production business Agent",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "pattern": "^PI-DEMO-", "maxLength": 100},
                "preferred_delivery": {"enum": ["standard", "express"]},
            },
            "required": ["order_id", "preferred_delivery"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": list(DemoResult.model_fields)},
        tools=list(names.values()),
        permissions=[f"tool:{name}" for name in names.values()],
        timeout_seconds=180,
        max_concurrency=1,
        retry_policy={"max_attempts": 1, "backoff_seconds": 0},
        runtime={
            "type": "pi",
            "instructions": prompt,
            "max_turns": 8,
            "tool_execution": "sequential",
            "output_type": "agent_hub.pi_multistep_demo:DemoResult",
            "model_config": {
                "model": model_name,
                "max_tokens": 1600,
                "max_retries": 0,
                "timeout_seconds": 40,
                "fallback_models": [],
                "response_format": None,
            },
        },
        enabled=False,
        owner="pi-demo",
        managed_by="pi-demo",
    )
    limited = spec.model_copy(deep=True, update={"version": "1.0.1"})
    limited.runtime["max_turns"] = 1
    return [spec, limited], tool_specs
