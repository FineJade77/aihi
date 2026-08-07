"""Serializable semantic events emitted and persisted by the runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from aiharness.core.ids import new_id
from aiharness.core.schema import EVENT_SCHEMA_VERSION, upgrade_event_payload


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    id: str = field(default_factory=lambda: new_id("evt"))
    seq: int | None = None
    created_at: str = field(default_factory=utc_now)
    ephemeral: bool = False
    schema_version: int = EVENT_SCHEMA_VERSION

    def persisted(self, seq: int) -> Event:
        return replace(self, seq=seq, ephemeral=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "created_at": self.created_at,
            "ephemeral": self.ephemeral,
            "schema_version": self.schema_version,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Event:
        # Fail closed on an envelope this harness does not understand rather
        # than read a future payload as if it were current.
        value = upgrade_event_payload(value)
        seq = value.get("seq")
        return cls(
            id=str(value["id"]),
            type=str(value["type"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]) if value.get("run_id") is not None else None,
            seq=int(seq) if seq is not None else None,
            created_at=str(value["created_at"]),
            ephemeral=bool(value.get("ephemeral", False)),
            schema_version=int(value.get("schema_version", EVENT_SCHEMA_VERSION)),
            data=dict(value.get("data", {})),
        )
