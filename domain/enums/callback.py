"""CallbackStatus — delivery state of a run's outbound callback.

SKIPPED means no callback was needed (e.g. none configured), as opposed to
FAILED which means delivery was attempted but did not succeed.
"""

from __future__ import annotations

from enum import StrEnum


class CallbackStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
