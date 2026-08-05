"""Explicit Skill trust records and atomic lockfile persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aiharness.skills.discovery import SkillCandidate, SkillDiscovery, SkillScope
from aiharness.skills.errors import SkillIntegrityError, SkillNotTrusted

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SkillTrustRecord:
    skill_name: str
    version: str
    scope: SkillScope
    content_sha256: str
    trusted_by: str
    trusted_at: str
    enabled: bool = False

    @property
    def key(self) -> str:
        return f"{self.skill_name}@{self.version}:{self.scope.value}"

    def __post_init__(self) -> None:
        if not self.skill_name.strip() or not self.version.strip() or not self.trusted_by.strip():
            raise SkillIntegrityError("Skill trust record identity and trusted_by are required")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise SkillIntegrityError("Skill trust record content hash is invalid")
        if not isinstance(self.enabled, bool):
            raise SkillIntegrityError("Skill trust record enabled must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_name": self.skill_name,
            "version": self.version,
            "scope": self.scope.value,
            "content_sha256": self.content_sha256,
            "trusted_by": self.trusted_by,
            "trusted_at": self.trusted_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: object) -> SkillTrustRecord:
        if not isinstance(value, dict):
            raise SkillIntegrityError("Skill trust record must be an object")
        required = (
            "skill_name",
            "version",
            "scope",
            "content_sha256",
            "trusted_by",
            "trusted_at",
        )
        if any(not isinstance(value.get(key), str) for key in required):
            raise SkillIntegrityError("Skill trust record has invalid fields")
        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise SkillIntegrityError("Skill trust record enabled must be boolean")
        try:
            scope = SkillScope(value["scope"])
        except ValueError as exc:
            raise SkillIntegrityError("Skill trust record scope is invalid") from exc
        return cls(
            skill_name=value["skill_name"],
            version=value["version"],
            scope=scope,
            content_sha256=value["content_sha256"],
            trusted_by=value["trusted_by"],
            trusted_at=value["trusted_at"],
            enabled=enabled,
        )


class SkillTrustStore(Protocol):
    def list_records(self) -> tuple[SkillTrustRecord, ...]: ...

    def get(self, skill_name: str, version: str, scope: SkillScope) -> SkillTrustRecord | None: ...

    def put(self, record: SkillTrustRecord) -> None: ...

    def remove(self, skill_name: str, version: str, scope: SkillScope) -> None: ...


class InMemorySkillTrustStore:
    def __init__(self, records: Iterable[SkillTrustRecord] = ()) -> None:
        self._records = {
            (record.skill_name, record.version, record.scope): record for record in records
        }

    def list_records(self) -> tuple[SkillTrustRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records, key=str))

    def get(self, skill_name: str, version: str, scope: SkillScope) -> SkillTrustRecord | None:
        return self._records.get((skill_name, version, scope))

    def put(self, record: SkillTrustRecord) -> None:
        self._records[(record.skill_name, record.version, record.scope)] = record

    def remove(self, skill_name: str, version: str, scope: SkillScope) -> None:
        self._records.pop((skill_name, version, scope), None)


class FileSkillTrustStore(InMemorySkillTrustStore):
    """Atomic JSON lockfile; malformed records fail closed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(self._load())

    def put(self, record: SkillTrustRecord) -> None:
        super().put(record)
        self._save()

    def remove(self, skill_name: str, version: str, scope: SkillScope) -> None:
        super().remove(skill_name, version, scope)
        self._save()

    def _load(self) -> tuple[SkillTrustRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillIntegrityError("Skill trust lockfile is invalid") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise SkillIntegrityError("Unsupported Skill trust lockfile schema")
        records = raw.get("skills", [])
        if not isinstance(records, list):
            raise SkillIntegrityError("Skill trust lockfile skills must be an array")
        parsed = tuple(SkillTrustRecord.from_dict(item) for item in records)
        keys = [record.key for record in parsed]
        if len(set(keys)) != len(keys):
            raise SkillIntegrityError("Skill trust lockfile contains duplicate identities")
        return parsed

    def _save(self) -> None:
        payload = {
            "schema_version": 1,
            "skills": [record.to_dict() for record in self.list_records()],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)


@dataclass(frozen=True, slots=True)
class SkillStatus:
    candidate: SkillCandidate
    trusted: bool
    enabled: bool
    reason: str | None = None

    @property
    def loadable(self) -> bool:
        return self.trusted and self.enabled


class SkillTrustManager:
    def __init__(self, store: SkillTrustStore, *, discovery: SkillDiscovery | None = None) -> None:
        self.store = store
        self.discovery = discovery

    def status(self, candidate: SkillCandidate) -> SkillStatus:
        record = self.store.get(
            candidate.frontmatter.name,
            candidate.frontmatter.version,
            candidate.scope,
        )
        if (
            record is None
            or record.content_sha256 != candidate.content_sha256
        ):
            return SkillStatus(candidate, trusted=False, enabled=False, reason="hash_not_trusted")
        return SkillStatus(candidate, trusted=True, enabled=record.enabled)

    def trust(
        self, candidate: SkillCandidate, *, trusted_by: str, enable: bool = False
    ) -> SkillTrustRecord:
        if not trusted_by.strip():
            raise SkillNotTrusted("trusted_by must be explicit")
        record = SkillTrustRecord(
            skill_name=candidate.frontmatter.name,
            version=candidate.frontmatter.version,
            scope=candidate.scope,
            content_sha256=candidate.content_sha256,
            trusted_by=trusted_by,
            trusted_at=datetime.now(UTC).isoformat(),
            enabled=enable,
        )
        self.store.put(record)
        return record

    def enable(self, candidate: SkillCandidate) -> SkillTrustRecord:
        record = self.store.get(
            candidate.frontmatter.name,
            candidate.frontmatter.version,
            candidate.scope,
        )
        if record is None or record.content_sha256 != candidate.content_sha256:
            raise SkillNotTrusted(f"Skill hash is not trusted: {candidate.versioned_key}")
        enabled = SkillTrustRecord(
            skill_name=record.skill_name,
            version=record.version,
            scope=record.scope,
            content_sha256=record.content_sha256,
            trusted_by=record.trusted_by,
            trusted_at=record.trusted_at,
            enabled=True,
        )
        self.store.put(enabled)
        return enabled

    def disable(self, candidate: SkillCandidate) -> None:
        record = self.store.get(
            candidate.frontmatter.name,
            candidate.frontmatter.version,
            candidate.scope,
        )
        if record is None or record.content_sha256 != candidate.content_sha256:
            return
        self.store.put(
            SkillTrustRecord(
                skill_name=record.skill_name,
                version=record.version,
                scope=record.scope,
                content_sha256=record.content_sha256,
                trusted_by=record.trusted_by,
                trusted_at=record.trusted_at,
                enabled=False,
            )
        )

    def require_loadable(
        self, candidate: SkillCandidate, *, discovery: SkillDiscovery | None = None
    ) -> SkillCandidate:
        verifier = discovery or self.discovery
        if verifier is None:
            raise SkillIntegrityError("A fresh SkillDiscovery is required before loading")
        effective = next(
            (item for item in verifier.discover() if item.key == candidate.key), None
        )
        if (
            effective is None
            or effective.frontmatter.version != candidate.frontmatter.version
            or effective.scope != candidate.scope
            or effective.root != candidate.root
            or effective.content_sha256 != candidate.content_sha256
        ):
            raise SkillIntegrityError(
                f"Skill is not the current effective candidate: {candidate.versioned_key}",
                details={"reason": "shadowed_or_replaced"},
            )
        candidate = verifier.verify(effective)
        status = self.status(candidate)
        if not status.loadable:
            raise SkillNotTrusted(
                f"Skill is not loadable: {candidate.versioned_key}",
                details={"reason": status.reason or "disabled"},
            )
        return candidate


__all__ = [
    "FileSkillTrustStore",
    "InMemorySkillTrustStore",
    "SkillStatus",
    "SkillTrustManager",
    "SkillTrustRecord",
    "SkillTrustStore",
]
