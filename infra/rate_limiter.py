"""In-process API rate limiting using a per-caller token-bucket algorithm."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TokenBucket:
    capacity: float
    fill_rate: float
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
        self.last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens


class RateLimiter:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, caller: str) -> TokenBucket:
        with self._lock:
            if caller not in self._buckets:
                caller_limits = self.settings.get("callers", {})
                default_limits = self.settings.get("default", {})
                limits = caller_limits.get(caller, default_limits)
                capacity = float(limits.get("capacity", 100))
                fill_rate = float(limits.get("fill_rate", 10))
                self._buckets[caller] = TokenBucket(capacity=capacity, fill_rate=fill_rate)
            return self._buckets[caller]

    def is_allowed(self, caller: str) -> bool:
        if not self.settings.get("enabled", False):
            return True
        return self._get_bucket(caller).consume()

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                caller: {"available": bucket.available, "capacity": bucket.capacity}
                for caller, bucket in self._buckets.items()
            }
