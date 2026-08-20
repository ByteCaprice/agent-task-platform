"""RetryPolicy — how many times and how long to wait between retries.

Defines max attempts and backoff, and computes the delay before a given
attempt (fixed or exponential).
"""

from __future__ import annotations

from pydantic import BaseModel


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff_seconds: float = 0.0
    backoff_type: str = "fixed"

    def delay_for_attempt(self, attempt: int) -> float:
        if self.backoff_type == "exponential":
            return self.backoff_seconds * (2 ** (attempt - 1))
        return self.backoff_seconds * attempt
