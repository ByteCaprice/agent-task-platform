"""Lifecycle states for one durable agent stage."""

from enum import StrEnum


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
