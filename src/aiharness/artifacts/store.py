"""Content-addressed storage for large tool output and other derived artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: str
    metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class ArtifactStore(Protocol):
    def put_text(
        self,
        content: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef: ...

    def read_text(self, artifact_id: str) -> str: ...


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
    ) -> ArtifactRef:
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        artifact_id = f"art-{digest[:32]}"
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
        created_at = datetime.now(UTC).isoformat()
        ref = ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            size_bytes=len(encoded),
            sha256=digest,
            created_at=created_at,
            metadata=dict(metadata or {}),
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

    def read_text(self, artifact_id: str) -> str:
        payload_path = self._payload_path(artifact_id)
        try:
            raw = payload_path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"Artifact not found: {artifact_id}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        try:
            manifest = json.loads(self._manifest_path(artifact_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Artifact manifest is missing or invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("sha256") != digest:
            raise ValueError("Artifact manifest integrity check failed")
        expected_id = f"art-{digest[:32]}"
        if expected_id != artifact_id:
            raise ValueError("Artifact integrity check failed")
        return raw.decode("utf-8")

    def _payload_path(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        return self.root / f"{artifact_id}.data"

    def _manifest_path(self, artifact_id: str) -> Path:
        self._validate_id(artifact_id)
        return self.root / f"{artifact_id}.json"

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if not artifact_id.startswith("art-") or "/" in artifact_id or "\\" in artifact_id:
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
