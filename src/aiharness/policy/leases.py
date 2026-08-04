"""Short-lived capability leases and approval grants."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiharness.core.errors import EventInvariantViolation
from aiharness.core.events import Event
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
        if not run_id:
            raise ValueError("Capability lease run_id must not be empty")
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

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> CapabilityLease:
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        raw_capabilities = value.get("capabilities", [])
        if not isinstance(raw_capabilities, list | tuple | set | frozenset):
            raise ValueError("Capability lease capabilities must be a collection")
        return cls(
            lease_id=str(value["lease_id"]),
            run_id=str(value["run_id"]),
            capabilities=frozenset(str(item) for item in raw_capabilities),
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class Approval:
    scope: str
    granted_by: str
    expires_at: datetime | None = None
    run_id: str | None = None
    approval_id: str = field(default_factory=lambda: new_id("approval"))

    @classmethod
    def issue(
        cls,
        scope: str,
        granted_by: str,
        *,
        run_id: str | None = None,
        ttl_seconds: float | None = None,
    ) -> Approval:
        if not scope:
            raise ValueError("Approval scope must not be empty")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("Approval TTL must be positive")
        expires_at = _now() + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        return cls(scope=scope, granted_by=granted_by, expires_at=expires_at, run_id=run_id)

    def active(self, *, now: datetime | None = None) -> bool:
        return self.expires_at is None or (now or _now()) < self.expires_at

    def covers(self, scope: str, *, now: datetime | None = None) -> bool:
        return self.active(now=now) and self.scope in {scope, "*"}

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "scope": self.scope,
            "granted_by": self.granted_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Approval:
        raw_expires_at = value.get("expires_at")
        expires_at = datetime.fromisoformat(str(raw_expires_at)) if raw_expires_at else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return cls(
            approval_id=str(value["approval_id"]),
            scope=str(value["scope"]),
            granted_by=str(value["granted_by"]),
            expires_at=expires_at,
            run_id=str(value["run_id"]) if value.get("run_id") is not None else None,
        )


@dataclass(slots=True)
class AuthorizationState:
    """Replayable authorization projection derived from immutable session events."""

    leases: dict[str, CapabilityLease] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    pending_approvals: dict[str, Approval] = field(default_factory=dict)
    seen_lease_ids: set[str] = field(default_factory=set)
    revoked_lease_ids: set[str] = field(default_factory=set)
    seen_approval_ids: set[str] = field(default_factory=set)
    resolved_approval_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> AuthorizationState:
        state = cls()
        for event in events:
            state.apply(event)
        return state

    def apply(self, event: Event) -> None:
        if event.type == "capability.lease.issued":
            raw_lease = event.data.get("lease")
            if not isinstance(raw_lease, dict):
                raise EventInvariantViolation("Capability lease event is missing lease payload")
            lease = CapabilityLease.from_dict(raw_lease)
            if not lease.run_id or event.run_id != lease.run_id:
                raise EventInvariantViolation("Capability lease event run_id mismatch")
            if lease.lease_id in self.seen_lease_ids or lease.lease_id in self.revoked_lease_ids:
                raise EventInvariantViolation(f"Duplicate capability lease: {lease.lease_id}")
            self.seen_lease_ids.add(lease.lease_id)
            self.leases[lease.lease_id] = lease
            return
        if event.type == "capability.lease.revoked":
            lease_id = event.data.get("lease_id")
            if not isinstance(lease_id, str) or event.run_id is None:
                raise EventInvariantViolation("Invalid capability lease revocation event")
            lease = self.leases.get(lease_id)
            if lease is None or lease.run_id != event.run_id:
                raise EventInvariantViolation(f"Unknown capability lease: {lease_id}")
            self.leases.pop(lease_id)
            self.revoked_lease_ids.add(lease_id)
            return
        if event.type == "approval.requested":
            raw_approval = event.data.get("approval")
            if not isinstance(raw_approval, dict):
                raise EventInvariantViolation("Approval request is missing approval payload")
            approval = Approval.from_dict(raw_approval)
            if not approval.run_id or event.run_id != approval.run_id:
                raise EventInvariantViolation("Approval request run_id mismatch")
            if (
                approval.approval_id in self.seen_approval_ids
                or approval.approval_id in self.resolved_approval_ids
            ):
                raise EventInvariantViolation(f"Duplicate approval: {approval.approval_id}")
            self.seen_approval_ids.add(approval.approval_id)
            self.pending_approvals[approval.approval_id] = approval
            return
        if event.type == "approval.resolved":
            raw_approval = event.data.get("approval")
            approval_id = event.data.get("approval_id")
            status = event.data.get("status")
            if status not in {"granted", "denied"} or not isinstance(raw_approval, dict):
                raise EventInvariantViolation("Invalid approval resolution event")
            if not isinstance(approval_id, str):
                raise EventInvariantViolation("Approval resolution is missing approval_id")
            approval = Approval.from_dict(raw_approval)
            pending = self.pending_approvals.get(approval_id)
            if (
                pending is None
                or pending != approval
                or approval.approval_id != approval_id
                or event.run_id != pending.run_id
            ):
                raise EventInvariantViolation(
                    f"Approval resolution has no matching pending request: {approval_id}"
                )
            if status == "granted" and approval is not None:
                self.approvals[approval_id] = approval
            self.pending_approvals.pop(approval_id, None)
            self.resolved_approval_ids.add(approval_id)

    def active_leases(
        self, run_id: str, *, now: datetime | None = None
    ) -> tuple[CapabilityLease, ...]:
        return tuple(
            lease
            for lease in self.leases.values()
            if lease.run_id == run_id and lease.active(now=now)
        )

    def active_approvals(
        self, run_id: str, *, now: datetime | None = None
    ) -> tuple[Approval, ...]:
        return tuple(
            approval
            for approval in self.approvals.values()
            if approval.run_id == run_id and approval.active(now=now)
        )

    def approval(self, approval_id: str) -> Approval | None:
        approval = self.approvals.get(approval_id) or self.pending_approvals.get(approval_id)
        return approval if approval is not None and approval.active() else None

    def pending_approval(self, approval_id: str) -> Approval | None:
        approval = self.pending_approvals.get(approval_id)
        return approval if approval is not None and approval.active() else None
