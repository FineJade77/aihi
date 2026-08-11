"""Layered Skill discovery and explicit on-demand loading."""

from aihi.agent.skills.context import SkillIndexContributor
from aihi.agent.skills.discovery import SkillCandidate, SkillDiscovery, SkillRoot, SkillScope
from aihi.agent.skills.errors import (
    SkillConflict,
    SkillError,
    SkillIntegrityError,
    SkillManifestError,
    SkillNotFound,
    SkillNotRequested,
    SkillNotTrusted,
)
from aihi.agent.skills.loader import LoadedSkill, SkillLoader
from aihi.agent.skills.manifest import SKILL_FILENAME, SkillFrontmatter, parse_skill_document
from aihi.agent.skills.trust import (
    FileSkillTrustStore,
    InMemorySkillTrustStore,
    SkillStatus,
    SkillTrustManager,
    SkillTrustRecord,
    SkillTrustStore,
)

__all__ = [
    "FileSkillTrustStore",
    "InMemorySkillTrustStore",
    "LoadedSkill",
    "SKILL_FILENAME",
    "SkillCandidate",
    "SkillConflict",
    "SkillDiscovery",
    "SkillError",
    "SkillFrontmatter",
    "SkillIndexContributor",
    "SkillIntegrityError",
    "SkillLoader",
    "SkillManifestError",
    "SkillNotFound",
    "SkillNotRequested",
    "SkillNotTrusted",
    "SkillRoot",
    "SkillScope",
    "SkillStatus",
    "SkillTrustManager",
    "SkillTrustRecord",
    "SkillTrustStore",
    "parse_skill_document",
]
