"""Short-lived capability leases and approval grants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiharness.core.ids import new_id


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CapabilityLease:
    lease_id: str
    run_id: str
    capabilities: frozenset[str]
    expires_at: datetime

    @classmethod
    def issue(
        cls,
        run_id: str,
        capabilities: frozenset[str] | set[str] | tuple[str, ...],
        *,
        ttl_seconds: float = 300.0,
    ) -> CapabilityLease:
        if ttl_seconds <= 0:
            raise ValueError("Capability lease TTL must be positive")
        return cls(
            lease_id=new_id("lease"),
            run_id=run_id,
            capabilities=frozenset(capabilities),
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )

    def active(self, *, now: datetime | None = None) -> bool:
        return (now or _now()) < self.expires_at

    def grants(self, required: tuple[str, ...], *, now: datetime | None = None) -> bool:
        return self.active(now=now) and set(required).issubset(self.capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "capabilities": sorted(self.capabilities),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Approval:
    scope: str
    granted_by: str
    expires_at: datetime | None = None

    def active(self, *, now: datetime | None = None) -> bool:
        return self.expires_at is None or (now or _now()) < self.expires_at

    def covers(self, scope: str, *, now: datetime | None = None) -> bool:
        return self.active(now=now) and self.scope in {scope, "*"}

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "granted_by": self.granted_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
