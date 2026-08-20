"""``SubprocessPythonAgent``: runs a Python agent in an isolated child process,
passing input and receiving output as JSON over stdin/stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from framework.runtime.context import AgentContext


class SubprocessPythonAgent:
    def __init__(self, runtime: dict[str, Any]) -> None:
        self.runtime = runtime

    async def run(self, context: AgentContext, input_data: dict[str, Any]) -> dict[str, Any]:
        target = self.runtime["target"]
        payload = {
            "input": input_data,
            "context": {
                "run_id": context.run_id,
                "route_tag": context.route_tag,
                "trace_id": context.trace_id,
                "metadata": context.metadata,
                "files": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in context.files
                ],
                "agent": context.agent.model_dump(mode="json"),
            },
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "framework.runtime.isolation.subprocess_runner",
            target,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
        stdout, stderr = await process.communicate(json.dumps(payload, ensure_ascii=False).encode())
        if process.returncode != 0:
            error = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"isolated python agent failed with exit_code={process.returncode}: {error}")
        try:
            data = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise RuntimeError("isolated python agent returned invalid JSON") from exc
        return data if isinstance(data, dict) else {"data": data}
