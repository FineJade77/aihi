"""Explicit, trusted, on-demand Skill body loading."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from aiharness.skills.discovery import (
    SkillCandidate,
    SkillDiscovery,
    SkillScope,
    _read_regular_file,
)
from aiharness.skills.errors import SkillIntegrityError, SkillNotFound, SkillNotRequested
from aiharness.skills.manifest import SkillFrontmatter, parse_skill_document
from aiharness.skills.trust import SkillTrustManager


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    name: str
    version: str
    scope: SkillScope
    frontmatter: SkillFrontmatter
    body: str
    content_sha256: str


class SkillLoader:
    """Bodies are returned only after explicit request and exact trust verification."""

    def __init__(self, trust_manager: SkillTrustManager, *, discovery: SkillDiscovery) -> None:
        self.trust_manager = trust_manager
        self.discovery = discovery

    def load(
        self,
        candidate: SkillCandidate,
        *,
        requested: bool,
        discovery: SkillDiscovery | None = None,
    ) -> LoadedSkill:
        if requested is not True:
            raise SkillNotRequested(
                f"Skill body requires an explicit request: {candidate.frontmatter.name}"
            )
        verifier = discovery or self.discovery
        candidate = self.trust_manager.require_loadable(candidate, discovery=verifier)
        raw = _read_regular_file(candidate.document_path, max_bytes=verifier.max_skill_bytes)
        frontmatter, body = parse_skill_document(
            raw, max_frontmatter_bytes=verifier.max_frontmatter_bytes
        )
        content_sha256 = hashlib.sha256(raw).hexdigest()
        if content_sha256 != candidate.content_sha256 or frontmatter != candidate.frontmatter:
            raise SkillIntegrityError(
                f"Skill changed while loading: {candidate.versioned_key}",
                details={
                    "expected_content_sha256": candidate.content_sha256,
                    "actual_content_sha256": content_sha256,
                },
            )
        return LoadedSkill(
            name=frontmatter.name,
            version=frontmatter.version,
            scope=candidate.scope,
            frontmatter=frontmatter,
            body=body,
            content_sha256=content_sha256,
        )

    def load_by_name(
        self,
        skill_name: str,
        *,
        requested: bool,
        discovery: SkillDiscovery | None = None,
    ) -> LoadedSkill:
        verifier = discovery or self.discovery
        candidate = next(
            (item for item in verifier.discover() if item.frontmatter.name == skill_name), None
        )
        if candidate is None:
            raise SkillNotFound(f"Skill was not discovered: {skill_name}")
        return self.load(candidate, requested=requested, discovery=verifier)


__all__ = ["LoadedSkill", "SkillLoader"]
