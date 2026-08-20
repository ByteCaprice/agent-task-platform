"""``HTTPAgent``: adapter that runs an agent by POSTing the run input to a remote
HTTP endpoint and returning the endpoint's JSON response as the agent output.
"""

from __future__ import annotations

from typing import Any

from domain import LogEvent
from framework.runtime.context import AgentContext
from framework.runtime.utils import headers_for_http_runtime


class HTTPAgent:
    def __init__(self, runtime: dict[str, Any], http_client: Any) -> None:
        self.runtime = runtime
        self.http_client = http_client

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        url = self.runtime.get("url") or self.runtime.get("endpoint")
        if not url:
            raise RuntimeError("HTTP Agent runtime requires url or endpoint")
        method = self.runtime.get("method", "POST").upper()
        payload = {
            "run_id": context.run_id,
            "route_tag": context.route_tag,
            "trace_id": context.trace_id,
            "input": input_data,
            "metadata": context.metadata,
            "files": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in context.files],
            "agent": {"name": context.agent.name, "version": context.agent.version},
        }
        if self.runtime.get("payload_template"):
            payload.update(self.runtime["payload_template"])
        headers = headers_for_http_runtime(self.runtime)
        context.state_client.add_log(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component="agent_runtime",
                event_type="remote_agent_call_started",
                message="Remote HTTP Agent call started",
                data={"agent": context.agent.name, "url": url, "method": method},
            )
        )
        try:
            response = await self.http_client.request(method, url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            context.state_client.add_log(
                LogEvent(
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                    component="agent_runtime",
                    event_type="remote_agent_call_failed",
                    message="Remote HTTP Agent call failed",
                    data={"agent": context.agent.name, "url": url, "error": f"{type(exc).__name__}: {exc}"},
                )
            )
            raise
        output = data if isinstance(data, dict) else {"data": data}
        output.setdefault("agent", {"name": context.agent.name, "version": context.agent.version})
        context.state_client.add_log(
            LogEvent(
                run_id=context.run_id,
                trace_id=context.trace_id,
                component="agent_runtime",
                event_type="remote_agent_call_succeeded",
                message="Remote HTTP Agent call succeeded",
                data={"agent": context.agent.name, "url": url, "output": output},
            )
        )
        return output
