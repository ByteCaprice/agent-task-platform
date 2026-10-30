"""Persistence errors exposed without coupling use cases to a database driver."""


class UniqueConstraintConflictError(Exception):
    """A transaction was rolled back because a unique key already exists."""
