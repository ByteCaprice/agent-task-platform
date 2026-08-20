"""Process-local resilience primitives for the tool gateway.

Two self-contained state machines extracted from :class:`ToolGateway` so each
can be reasoned about and unit-tested in isolation:

- :class:`QpsLimiter` — sliding-window per-tool rate throttle.
- :class:`CircuitBreaker` — per-tool failure counter with cooldown-based open state.

Both are *process-local*; cluster-wide coordination is layered on separately by
the gateway via the coordination backend.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any


def _new_breaker_state() -> dict[str, float | int]:
    return {"failures": 0, "open_until": 0.0}


class QpsLimiter:
    """Sliding 1-second window per-tool QPS throttle."""

    def __init__(self, tool_names: list[str] | None = None) -> None:
        self._lock = asyncio.Lock()
        self._windows: dict[str, deque[float]] = {name: deque() for name in (tool_names or [])}

    async def acquire(self, tool_name: str, qps: float | None) -> None:
        """Block until a request slot is available within the tool's QPS budget."""
        if not qps:
            return
        while True:
            async with self._lock:
                window = self._windows.setdefault(tool_name, deque())
                now = time.monotonic()
                while window and now - window[0] >= 1:
                    window.popleft()
                if len(window) < qps:
                    window.append(now)
                    return
                sleep_for = max(0.0, 1 - (now - window[0]))
            await asyncio.sleep(sleep_for)


class CircuitBreaker:
    """Per-tool failure counter that opens for a cooldown after a threshold."""

    def __init__(self, tool_names: list[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, float | int]] = {name: _new_breaker_state() for name in (tool_names or [])}

    def open_until(self, tool_name: str) -> float:
        """Monotonic time until which the breaker is open (0.0 == closed)."""
        with self._lock:
            return float(self._state.setdefault(tool_name, _new_breaker_state()).get("open_until", 0.0))

    def record_failure(self, tool_name: str, config: dict[str, Any]) -> float | None:
        """Count a failure; return the new ``open_until`` if this trips the breaker."""
        if not config or config.get("enabled") is False:
            return None
        threshold = int(config.get("failure_threshold", 3))
        cooldown_seconds = float(config.get("cooldown_seconds", 30))
        with self._lock:
            state = self._state.setdefault(tool_name, _new_breaker_state())
            state["failures"] = int(state.get("failures", 0)) + 1
            if int(state["failures"]) >= threshold:
                state["open_until"] = time.monotonic() + cooldown_seconds
                return float(state["open_until"])
        return None

    def record_success(self, tool_name: str) -> None:
        with self._lock:
            self._state[tool_name] = _new_breaker_state()

    def snapshot(self, tool_name: str) -> dict[str, Any]:
        """Return ``{failures, open_until}`` for metrics reporting."""
        with self._lock:
            state = self._state.setdefault(tool_name, _new_breaker_state())
            return {"failures": int(state.get("failures", 0)), "open_until": float(state.get("open_until", 0.0))}
