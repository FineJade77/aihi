"""Content-addressed storage for large tool output and other derived artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

ArtifactRetention = Literal["run", "session", "persistent"]


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    """Ownership and retention metadata persisted with an artifact manifest."""

    session_id: str | None = None
    run_id: str | None = None
    retention: ArtifactRetention = "persistent"
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if self.run_id is not None and self.session_id is None:
            raise ValueError("run-scoped artifacts require a session_id")
        if self.retention not in {"run", "session", "persistent"}:
            raise ValueError(f"Unsupported artifact retention: {self.retention}")
        if self.retention == "persistent" and (
            self.session_id is not None or self.run_id is not None
        ):
            raise ValueError("persistent artifacts cannot have an owner scope")
        if self.retention == "session" and (
            self.session_id is None or self.run_id is not None
        ):
            raise ValueError("session retention requires only a session_id")
        if self.retention == "run" and (self.session_id is None or self.run_id is None):
            raise ValueError("run retention requires session_id and run_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "retention": self.retention,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> ArtifactPolicy:
        if not isinstance(value, dict):
            return cls()
        retention = str(value.get("retention", "persistent"))
        if retention not in {"run", "session", "persistent"}:
            retention = "persistent"
        return cls(
            session_id=(str(value["session_id"]) if value.get("session_id") else None),
            run_id=(str(value["run_id"]) if value.get("run_id") else None),
            retention=retention,  # type: ignore[arg-type]
            expires_at=(str(value["expires_at"]) if value.get("expires_at") else None),
        )


@dataclass(frozen=True, slots=True)
class ArtifactAccess:
    """Runtime capability used to read or remove scoped artifacts."""

    session_id: str | None = None
    run_id: str | None = None
    allow_delete: bool = False
    admin: bool = False

    def can_access(self, policy: ArtifactPolicy) -> bool:
        if self.admin or (policy.session_id is None and policy.run_id is None):
            return True
        if self.session_id != policy.session_id:
            return False
        return policy.run_id is None or self.run_id == policy.run_id

    def can_delete_artifact(self, policy: ArtifactPolicy) -> bool:
        return self.allow_delete and self.can_access(policy)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: str
    metadata: dict[str, object]
    policy: ArtifactPolicy = field(default_factory=ArtifactPolicy)

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "policy": self.policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ArtifactRef:
        metadata = value.get("metadata", {})
        return cls(
            artifact_id=str(value.get("artifact_id", "")),
            media_type=str(value.get("media_type", "application/octet-stream")),
            size_bytes=int(value.get("size_bytes", 0)),
            sha256=str(value.get("sha256", "")),
            created_at=str(value.get("created_at", "")),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            policy=ArtifactPolicy.from_dict(value.get("policy")),
        )


class ArtifactStore(Protocol):
    def put_text(
        self,
        content: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        metadata: Mapping[str, object] | None = None,
        policy: ArtifactPolicy | None = None,
        expires_in_seconds: float | None = None,
    ) -> ArtifactRef: ...

    def get_ref(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccess | None = None,
        allow_expired: bool = False,
    ) -> ArtifactRef: ...

    def read_text(self, artifact_id: str, *, access: ArtifactAccess | None = None) -> str: ...

    def list_refs(
        self,
        *,
        access: ArtifactAccess | None = None,
        include_expired: bool = False,
    ) -> tuple[ArtifactRef, ...]: ...

    def delete(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccess,
        allow_expired: bool = False,
    ) -> ArtifactRef: ...

    def cleanup_expired(
        self, *, now: datetime | None = None, access: ArtifactAccess
    ) -> tuple[ArtifactRef, ...]: ...


class FileArtifactStore:
    """Local atomic artifact store with content-addressed immutable payloads."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError(f"Artifact root is not a directory: {self.root}")

    def put_text(
        self,
        content: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        metadata: Mapping[str, object] | None = None,
        policy: ArtifactPolicy | None = None,
        expires_in_seconds: float | None = None,
    ) -> ArtifactRef:
        policy = policy or ArtifactPolicy()
        if expires_in_seconds is not None:
            if expires_in_seconds < 0:
                raise ValueError("expires_in_seconds cannot be negative")
            policy = replace(
                policy,
                expires_at=(
                    datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
                ).isoformat(),
            )
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        artifact_id = self._artifact_id(digest, policy)
        payload_path = self._payload_path(artifact_id)
        manifest_path = self._manifest_path(artifact_id)
        payload_is_valid = False
        if payload_path.exists():
            try:
                payload_is_valid = hashlib.sha256(payload_path.read_bytes()).hexdigest() == digest
            except OSError:
                payload_is_valid = False
        if not payload_is_valid:
            self._atomic_write(payload_path, encoded)
        existing = self._load_ref_if_valid(artifact_id, digest, len(encoded))
        if existing is not None:
            return existing
        created_at = datetime.now(UTC).isoformat()
        ref = ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(encoded),
            sha256=digest,
            created_at=created_at,
            metadata=dict(metadata or {}),
            policy=policy,
        )
        manifest_is_valid = False
        if manifest_path.exists():
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_is_valid = (
                    isinstance(raw_manifest, dict)
                    and raw_manifest.get("sha256") == digest
                    and raw_manifest.get("size_bytes") == len(encoded)
                )
            except (OSError, json.JSONDecodeError):
                manifest_is_valid = False
        if not manifest_is_valid:
            self._atomic_write(
                manifest_path,
                json.dumps(ref.to_dict(), ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                ),
            )
        return ref

    def get_ref(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccess | None = None,
        allow_expired: bool = False,
    ) -> ArtifactRef:
        self._validate_id(artifact_id)
        ref = self._load_ref(artifact_id)
        self._require_access(ref, access)
        if not allow_expired and self._is_expired(ref):
            raise ValueError("Artifact has expired")
        return ref

    def read_text(self, artifact_id: str, *, access: ArtifactAccess | None = None) -> str:
        ref = self.get_ref(artifact_id, access=access)
        payload_path = self._payload_path(artifact_id)
        try:
            raw = payload_path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"Artifact not found: {artifact_id}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if ref.sha256 != digest or ref.size_bytes != len(raw):
            raise ValueError("Artifact manifest integrity check failed")
        expected_id = self._artifact_id(digest, ref.policy)
        if expected_id != artifact_id:
            raise ValueError("Artifact integrity check failed")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Artifact is not valid UTF-8 text") from exc

    def list_refs(
        self,
        *,
        access: ArtifactAccess | None = None,
        include_expired: bool = False,
    ) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        for manifest_path in sorted(self.root.glob("art-*.json")):
            artifact_id = manifest_path.stem
            try:
                ref = self._load_ref(artifact_id)
                self._require_access(ref, access)
                if not include_expired and self._is_expired(ref):
                    continue
            except (OSError, ValueError, PermissionError):
                continue
            refs.append(ref)
        return tuple(refs)

    def delete(
        self,
        artifact_id: str,
        *,
        access: ArtifactAccess,
        allow_expired: bool = False,
    ) -> ArtifactRef:
        ref = self.get_ref(artifact_id, access=access, allow_expired=allow_expired)
        if not isinstance(access, ArtifactAccess) or not access.can_delete_artifact(ref.policy):
            raise PermissionError("Artifact delete capability is required")
        self._payload_path(artifact_id).unlink(missing_ok=True)
        self._manifest_path(artifact_id).unlink(missing_ok=True)
        return ref

    def cleanup_expired(
        self, *, now: datetime | None = None, access: ArtifactAccess
    ) -> tuple[ArtifactRef, ...]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        deleted: list[ArtifactRef] = []
        for ref in self.list_refs(access=access, include_expired=True):
            if ref.policy.expires_at is None:
                continue
            try:
                expires_at = datetime.fromisoformat(ref.policy.expires_at)
            except ValueError:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= current:
                deleted.append(self.delete(ref.artifact_id, access=access, allow_expired=True))
        return tuple(deleted)

    @staticmethod
    def _is_expired(ref: ArtifactRef, *, now: datetime | None = None) -> bool:
        if ref.policy.expires_at is None:
            return False
        try:
            expires_at = datetime.fromisoformat(ref.policy.expires_at)
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return expires_at <= current

    def _load_ref(self, artifact_id: str) -> ArtifactRef:
        manifest_path = self._manifest_path(artifact_id)
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Artifact manifest is missing or invalid") from exc
        if not isinstance(raw_manifest, dict):
            raise ValueError("Artifact manifest is missing or invalid")
        try:
            ref = ArtifactRef.from_dict(raw_manifest)
        except (TypeError, ValueError) as exc:
            raise ValueError("Artifact manifest fields are invalid") from exc
        if ref.artifact_id != artifact_id or ref.sha256 == "":
            raise ValueError("Artifact manifest identity is invalid")
        if self._artifact_id(ref.sha256, ref.policy) != artifact_id:
            raise ValueError("Artifact manifest identity is invalid")
        return ref

    def _load_ref_if_valid(
        self, artifact_id: str, digest: str, size_bytes: int
    ) -> ArtifactRef | None:
        try:
            ref = self._load_ref(artifact_id)
        except (OSError, ValueError):
            return None
        if ref.sha256 == digest and ref.size_bytes == size_bytes and ref.artifact_id == artifact_id:
            return ref
        return None

    @staticmethod
    def _require_access(ref: ArtifactRef, access: ArtifactAccess | None) -> None:
        if ref.policy.session_id is None and ref.policy.run_id is None:
            return
        if not isinstance(access, ArtifactAccess) or not access.can_access(ref.policy):
            raise PermissionError("Artifact access is outside its declared scope")

    @staticmethod
    def _artifact_id(digest: str, policy: ArtifactPolicy) -> str:
        suffix = ""
        if policy.session_id is not None or policy.run_id is not None:
            scope = f"{policy.session_id or ''}\0{policy.run_id or ''}"
            suffix = f"-{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:12]}"
        return f"art-{digest[:32]}{suffix}"

    def _payload_path(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        return self.root / f"{artifact_id}.data"

    def _manifest_path(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        return self.root / f"{artifact_id}.json"

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if re.fullmatch(r"art-[0-9a-f]{32}(?:-[0-9a-f]{12})?", artifact_id) is None:
            raise ValueError("Invalid artifact id")

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
