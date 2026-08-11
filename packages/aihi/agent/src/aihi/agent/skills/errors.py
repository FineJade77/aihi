"""Stable errors for Skill discovery, trust, and loading."""

from __future__ import annotations

from aihi.agent._core.errors import AgentRuntimeError


class SkillError(AgentRuntimeError):
    code = "skill_error"


class SkillManifestError(SkillError):
    code = "skill_manifest_invalid"


class SkillIntegrityError(SkillError):
    code = "skill_integrity_failed"


class SkillConflict(SkillError):
    code = "skill_conflict"


class SkillNotTrusted(SkillError):
    code = "skill_not_trusted"


class SkillNotRequested(SkillError):
    code = "skill_not_requested"


class SkillNotFound(SkillError):
    code = "skill_not_found"


__all__ = [
    "SkillConflict",
    "SkillError",
    "SkillIntegrityError",
    "SkillManifestError",
    "SkillNotFound",
    "SkillNotRequested",
    "SkillNotTrusted",
]
