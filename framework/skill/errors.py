"""Errors raised while resolving and using deployed Skills."""

from __future__ import annotations


class SkillError(ValueError):
    """Base class for Skill configuration and runtime errors."""


class SkillNotFoundError(SkillError):
    pass


class SkillDisabledError(SkillError):
    pass


class SkillValidationError(SkillError):
    pass


class SkillNotAssignedError(SkillError):
    pass


class SkillActivationDeniedError(SkillError):
    pass


class SkillResourceError(SkillError):
    pass


class SkillScriptError(SkillError):
    pass


class SkillCompatibilityError(SkillError):
    pass
