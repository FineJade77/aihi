"""Skills that ship with the Coding Agent distribution.

BUILTIN Skills are trusted implicitly: their integrity is the package's
integrity, and by the time one is read the package's own code has already run.
Every other scope still requires explicit trust.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from aihi.agent import SkillRoot, SkillScope

BUILTIN_SKILL_NAMES = frozenset({"code_review", "debug", "test_writing", "refactor"})


def builtin_skill_directory() -> Path:
    """Return the packaged builtin Skill directory on the filesystem."""

    resource = files(__package__ or "aihi.code_agent.skills") / "builtin"
    return Path(str(resource))


def builtin_skill_root() -> SkillRoot:
    """The BUILTIN Skill root, discoverable without a trust lockfile."""

    return SkillRoot(builtin_skill_directory(), SkillScope.BUILTIN)


__all__ = ["BUILTIN_SKILL_NAMES", "builtin_skill_directory", "builtin_skill_root"]
