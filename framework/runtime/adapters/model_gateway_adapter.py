"""``ModelGatewayAgent``: thin adapter that runs an agent by delegating the run to
the configured model client (``ModelGateway``) with the agent's runtime config.
"""

from __future__ import annotations

from typing import Any

from framework.runtime.context import AgentContext
from framework.skill.catalog import compose_instructions


class ModelGatewayAgent:
    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        if context.model_client is None:
            raise RuntimeError("model_client is not configured")
        runtime = dict(self.runtime)
        base_instructions = runtime.get("instructions") or "Return a JSON object for the run input."
        composed = compose_instructions(
            base_instructions=base_instructions,
            session=context.skills,
            catalog_max_chars=int(runtime.get("skill_catalog_max_chars", 8_000)),
        )
        runtime["instructions"] = composed.instructions
        if composed.provenance:
            runtime["prompt_fingerprint_content"] = composed.fingerprint_content
            runtime["skill_provenance"] = composed.provenance
        return await context.model_client.complete(
            run_id=context.run_id,
            trace_id=context.trace_id,
            agent_name=context.agent.name,
            agent_version=context.agent.version,
            input_data=input_data,
            metadata=context.metadata,
            runtime=runtime,
        )
