"""Serializable semantic events emitted and persisted by the runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from aiharness.core.ids import new_id


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
    schema_version: int = 1

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
        seq = value.get("seq")
        return cls(
            id=str(value["id"]),
            type=str(value["type"]),
            session_id=str(value["session_id"]),
            run_id=str(value["run_id"]) if value.get("run_id") is not None else None,
            seq=int(seq) if seq is not None else None,
            created_at=str(value["created_at"]),
            ephemeral=bool(value.get("ephemeral", False)),
            schema_version=int(value.get("schema_version", 1)),
            data=dict(value.get("data", {})),
        )
