"""Subprocess entry point (run via ``python -m``): loads a ``module:factory``
agent, runs it on the JSON payload read from stdin, and writes the JSON result
to stdout; errors go to stderr with a non-zero exit code.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any


async def _run(target: str, payload: dict[str, Any]) -> dict[str, Any]:
    module_name, factory_name = target.split(":", 1)
    factory = getattr(importlib.import_module(module_name), factory_name)
    agent = factory()
    context = SimpleNamespace(**payload["context"])
    result = agent.run(context, payload.get("input", {}))
    if hasattr(result, "__await__"):
        result = await result
    return result if isinstance(result, dict) else {"data": result}


def main() -> int:
    try:
        target = sys.argv[1]
        payload = json.loads(sys.stdin.read())
        output = asyncio.run(_run(target, payload))
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
