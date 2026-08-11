"""Explicit plugin trust records and lockfile persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aihi.agent.plugins.discovery import PluginCandidate, PluginDiscovery
from aihi.agent.plugins.errors import PluginIntegrityError, PluginNotTrusted, PluginVersionConflict


@dataclass(frozen=True, slots=True)
class PluginTrustRecord:
    plugin_id: str
    version: str
    manifest_sha256: str
    content_sha256: str
    trusted_by: str
    trusted_at: str
    enabled: bool = False

    @property
    def key(self) -> str:
        return f"{self.plugin_id}@{self.version}"

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "content_sha256": self.content_sha256,
            "trusted_by": self.trusted_by,
            "trusted_at": self.trusted_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginTrustRecord:
        if not isinstance(value, dict):
            raise PluginIntegrityError("Plugin trust record must be an object")
        required = (
            "plugin_id",
            "version",
            "manifest_sha256",
            "content_sha256",
            "trusted_by",
            "trusted_at",
        )
        if any(not isinstance(value.get(key), str) for key in required):
            raise PluginIntegrityError("Plugin trust record has invalid fields")
        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise PluginIntegrityError("Plugin trust record enabled must be boolean")
        return cls(
            plugin_id=str(value["plugin_id"]),
            version=str(value["version"]),
            manifest_sha256=str(value["manifest_sha256"]),
            content_sha256=str(value["content_sha256"]),
            trusted_by=str(value["trusted_by"]),
            trusted_at=str(value["trusted_at"]),
            enabled=enabled,
        )


class TrustStore(Protocol):
    def list_records(self) -> tuple[PluginTrustRecord, ...]: ...

    def get(self, plugin_id: str, version: str) -> PluginTrustRecord | None: ...

    def put(self, record: PluginTrustRecord) -> None: ...

    def remove(self, plugin_id: str, version: str) -> None: ...


class InMemoryTrustStore:
    def __init__(self, records: Iterable[PluginTrustRecord] = ()) -> None:
        self._records = {(record.plugin_id, record.version): record for record in records}

    def list_records(self) -> tuple[PluginTrustRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def get(self, plugin_id: str, version: str) -> PluginTrustRecord | None:
        return self._records.get((plugin_id, version))

    def put(self, record: PluginTrustRecord) -> None:
        self._records[(record.plugin_id, record.version)] = record

    def remove(self, plugin_id: str, version: str) -> None:
        self._records.pop((plugin_id, version), None)


class FileTrustStore(InMemoryTrustStore):
    """Atomic JSON lockfile; malformed records fail closed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(self._load())

    def put(self, record: PluginTrustRecord) -> None:
        super().put(record)
        self._save()

    def remove(self, plugin_id: str, version: str) -> None:
        super().remove(plugin_id, version)
        self._save()

    def _load(self) -> tuple[PluginTrustRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginIntegrityError("Plugin trust lockfile is invalid") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise PluginIntegrityError("Unsupported plugin trust lockfile schema")
        records = raw.get("plugins", [])
        if not isinstance(records, list):
            raise PluginIntegrityError("Plugin trust lockfile plugins must be an array")
        parsed = tuple(PluginTrustRecord.from_dict(item) for item in records)
        keys = [(record.plugin_id, record.version) for record in parsed]
        if len(set(keys)) != len(keys):
            raise PluginIntegrityError("Plugin trust lockfile contains duplicate identities")
        return parsed

    def _save(self) -> None:
        payload = {
            "schema_version": 1,
            "plugins": [record.to_dict() for record in self.list_records()],
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
class PluginStatus:
    candidate: PluginCandidate
    trusted: bool
    enabled: bool
    reason: str | None = None

    @property
    def activatable(self) -> bool:
        return self.candidate.compatible and self.trusted and self.enabled


class PluginTrustManager:
    """Require exact hash trust and an independent enable decision."""

    def __init__(self, store: TrustStore, *, discovery: PluginDiscovery | None = None) -> None:
        self.store = store
        self.discovery = discovery

    def status(self, candidate: PluginCandidate) -> PluginStatus:
        record = self.store.get(candidate.manifest.plugin_id, str(candidate.manifest.version))
        if not candidate.compatible:
            return PluginStatus(candidate, trusted=False, enabled=False, reason="host_version")
        if (
            record is None
            or record.manifest_sha256 != candidate.manifest_sha256
            or record.content_sha256 != candidate.content_sha256
        ):
            return PluginStatus(candidate, trusted=False, enabled=False, reason="hash_not_trusted")
        return PluginStatus(candidate, trusted=True, enabled=record.enabled)

    def trust(
        self, candidate: PluginCandidate, *, trusted_by: str, enable: bool = False
    ) -> PluginTrustRecord:
        if not candidate.compatible:
            raise PluginVersionConflict(
                f"Plugin is incompatible with this Harness: {candidate.key}"
            )
        if not trusted_by.strip():
            raise PluginNotTrusted("trusted_by must be explicit")
        record = PluginTrustRecord(
            plugin_id=candidate.manifest.plugin_id,
            version=str(candidate.manifest.version),
            manifest_sha256=candidate.manifest_sha256,
            content_sha256=candidate.content_sha256,
            trusted_by=trusted_by,
            trusted_at=datetime.now(UTC).isoformat(),
            enabled=enable,
        )
        self.store.put(record)
        return record

    def enable(self, candidate: PluginCandidate) -> PluginTrustRecord:
        record = self.store.get(candidate.manifest.plugin_id, str(candidate.manifest.version))
        if (
            record is None
            or record.manifest_sha256 != candidate.manifest_sha256
            or record.content_sha256 != candidate.content_sha256
        ):
            raise PluginNotTrusted(f"Plugin hash is not trusted: {candidate.key}")
        enabled = PluginTrustRecord(
            plugin_id=record.plugin_id,
            version=record.version,
            manifest_sha256=record.manifest_sha256,
            content_sha256=record.content_sha256,
            trusted_by=record.trusted_by,
            trusted_at=record.trusted_at,
            enabled=True,
        )
        self.store.put(enabled)
        return enabled

    def disable(self, candidate: PluginCandidate) -> None:
        record = self.store.get(candidate.manifest.plugin_id, str(candidate.manifest.version))
        if (
            record is None
            or record.manifest_sha256 != candidate.manifest_sha256
            or record.content_sha256 != candidate.content_sha256
        ):
            return
        self.store.put(
            PluginTrustRecord(
                plugin_id=record.plugin_id,
                version=record.version,
                manifest_sha256=record.manifest_sha256,
                content_sha256=record.content_sha256,
                trusted_by=record.trusted_by,
                trusted_at=record.trusted_at,
                enabled=False,
            )
        )

    def require_activatable(
        self, candidate: PluginCandidate, *, discovery: PluginDiscovery | None = None
    ) -> PluginCandidate:
        verifier = discovery or self.discovery
        if verifier is None:
            raise PluginIntegrityError(
                "A fresh PluginDiscovery is required before activation"
            )
        candidate = verifier.verify(candidate)
        status = self.status(candidate)
        if not status.activatable:
            raise PluginNotTrusted(
                f"Plugin is not activatable: {candidate.key}",
                details={"reason": status.reason or "disabled"},
            )
        return candidate


__all__ = [
    "FileTrustStore",
    "InMemoryTrustStore",
    "PluginStatus",
    "PluginTrustManager",
    "PluginTrustRecord",
    "TrustStore",
]
