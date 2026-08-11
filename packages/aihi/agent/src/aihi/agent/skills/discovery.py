"""Layered, metadata-only discovery for SKILL.md documents."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from aihi.agent.skills.errors import SkillConflict, SkillIntegrityError, SkillManifestError
from aihi.agent.skills.manifest import SKILL_FILENAME, SkillFrontmatter, parse_skill_document


class SkillScope(StrEnum):
    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"
    WORKSPACE = "workspace"

    @property
    def priority(self) -> int:
        return {
            SkillScope.BUILTIN: 10,
            SkillScope.USER: 20,
            SkillScope.PROJECT: 30,
            SkillScope.WORKSPACE: 40,
        }[self]


@dataclass(frozen=True, slots=True)
class SkillRoot:
    path: Path
    scope: SkillScope

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if path.is_symlink():
            raise SkillIntegrityError(f"Symlinked Skill roots are not allowed: {path}")
        object.__setattr__(self, "path", path.resolve())
        object.__setattr__(self, "scope", SkillScope(self.scope))


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    root: Path
    document_path: Path
    scope: SkillScope
    frontmatter: SkillFrontmatter
    content_sha256: str
    size_bytes: int

    @property
    def key(self) -> str:
        return self.frontmatter.name

    @property
    def versioned_key(self) -> str:
        return f"{self.frontmatter.name}@{self.frontmatter.version}"


class SkillDiscovery:
    """Discover one-level skills while keeping Markdown bodies out of candidates."""

    def __init__(
        self,
        roots: tuple[SkillRoot, ...] | list[SkillRoot],
        *,
        max_skill_bytes: int = 1_048_576,
        max_frontmatter_bytes: int = 64 * 1024,
        max_total_bytes: int = 250 * 1024 * 1024,
    ) -> None:
        if not roots:
            raise ValueError("At least one Skill discovery root is required")
        if max_skill_bytes <= 0 or max_frontmatter_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("Skill discovery size limits must be positive")
        self.roots = tuple(roots)
        self.max_skill_bytes = max_skill_bytes
        self.max_frontmatter_bytes = max_frontmatter_bytes
        self.max_total_bytes = max_total_bytes

    def discover_all(self) -> tuple[SkillCandidate, ...]:
        candidates: list[SkillCandidate] = []
        total_bytes = 0
        for skill_root in self.roots:
            for root in self._skill_roots(skill_root):
                candidate = self._load_candidate(root, skill_root.scope)
                total_bytes += candidate.size_bytes
                if total_bytes > self.max_total_bytes:
                    raise SkillIntegrityError("Skill documents exceed the total size limit")
                candidates.append(candidate)
        return tuple(
            sorted(
                candidates,
                key=lambda item: (item.key, -item.scope.priority, str(item.document_path)),
            )
        )

    def discover(self) -> tuple[SkillCandidate, ...]:
        """Return the effective catalog after higher-scope shadowing."""

        selected: dict[str, SkillCandidate] = {}
        for candidate in self.discover_all():
            previous = selected.get(candidate.key)
            if previous is None or candidate.scope.priority > previous.scope.priority:
                selected[candidate.key] = candidate
            elif candidate.scope.priority == previous.scope.priority:
                raise SkillConflict(
                    f"Duplicate Skill identity in the same scope: {candidate.key}",
                    details={
                        "scope": candidate.scope.value,
                        "paths": [str(previous.document_path), str(candidate.document_path)],
                    },
                )
        return tuple(sorted(selected.values(), key=lambda item: item.key))

    def verify(self, candidate: SkillCandidate) -> SkillCandidate:
        """Re-read and re-hash a candidate immediately before trusted loading."""

        current = self._load_candidate(candidate.root, candidate.scope)
        if (
            current.key != candidate.key
            or current.frontmatter.version != candidate.frontmatter.version
            or current.scope != candidate.scope
            or current.content_sha256 != candidate.content_sha256
        ):
            raise SkillIntegrityError(
                f"Skill changed after discovery: {candidate.versioned_key}",
                details={
                    "expected_content_sha256": candidate.content_sha256,
                    "actual_content_sha256": current.content_sha256,
                },
            )
        return current

    def _skill_roots(self, skill_root: SkillRoot) -> tuple[Path, ...]:
        root = skill_root.path
        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            raise SkillIntegrityError(f"Skill discovery root is not a directory: {root}")
        direct_document = root / SKILL_FILENAME
        if direct_document.is_symlink():
            raise SkillIntegrityError(f"Symlinked SKILL.md is not allowed: {direct_document}")
        if direct_document.is_file():
            return (root,)
        if direct_document.exists():
            raise SkillIntegrityError(f"SKILL.md is not a regular file: {direct_document}")
        found: list[Path] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_symlink():
                raise SkillIntegrityError(f"Symlinked Skill roots are not allowed: {child}")
            if not child.is_dir():
                continue
            document = child / SKILL_FILENAME
            if document.is_symlink():
                raise SkillIntegrityError(f"Symlinked SKILL.md is not allowed: {document}")
            if document.is_file():
                found.append(child.resolve())
            elif document.exists():
                raise SkillIntegrityError(f"SKILL.md is not a regular file: {document}")
        return tuple(found)

    def _load_candidate(self, root: Path, scope: SkillScope) -> SkillCandidate:
        if root.is_symlink() or not root.is_dir():
            raise SkillIntegrityError(f"Skill root is not a directory: {root}")
        document_path = root / SKILL_FILENAME
        raw = _read_regular_file(document_path, max_bytes=self.max_skill_bytes)
        frontmatter, _ = parse_skill_document(
            raw, max_frontmatter_bytes=self.max_frontmatter_bytes
        )
        return SkillCandidate(
            root=root,
            document_path=document_path,
            scope=scope,
            frontmatter=frontmatter,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise SkillIntegrityError(f"Symlinked Skill files are not allowed: {path}")
    file_descriptor = -1
    try:
        file_descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SkillIntegrityError(f"Skill document is not a regular file: {path}")
        with os.fdopen(file_descriptor, "rb") as document_file:
            file_descriptor = -1
            raw = document_file.read(max_bytes + 1)
    except OSError as exc:
        raise SkillManifestError(f"Cannot read Skill document: {path}") from exc
    except SkillIntegrityError:
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    if len(raw) > max_bytes:
        raise SkillManifestError(f"SKILL.md exceeds the size limit: {path}")
    return raw


__all__ = ["SkillCandidate", "SkillDiscovery", "SkillRoot", "SkillScope"]
