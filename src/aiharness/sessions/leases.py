"""Fenced, run-scoped worker leases.

Leases are control-plane state, not an authorization grant.  A worker must
still pass the normal policy/tool/sandbox chain for every side effect.  The
fencing token makes a stale worker unable to renew or release a lease after a
different worker has taken it over.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiharness.core.errors import LeaseConflict, LeaseNotFound
from aiharness.core.ids import new_id


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lease timestamps must be ISO-8601") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunLease:
    lease_id: str
    run_id: str
    owner_id: str
    expires_at: str
    fencing_token: int
    created_at: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.lease_id, self.run_id, self.owner_id)
        ):
            raise ValueError("lease_id, run_id, and owner_id are required")
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token <= 0
        ):
            raise ValueError("fencing_token must be positive")
        if not isinstance(self.expires_at, str) or not isinstance(self.created_at, str):
            raise ValueError("lease timestamps must be strings")
        _parse_time(self.expires_at)
        _parse_time(self.created_at)

    @property
    def active(self) -> bool:
        return self.is_active()

    def is_active(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return _parse_time(self.expires_at) > current

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "expires_at": self.expires_at,
            "fencing_token": self.fencing_token,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RunLease:
        if not isinstance(value, dict):
            raise ValueError("lease must be a JSON object")
        return cls(
            lease_id=value["lease_id"],  # type: ignore[arg-type]
            run_id=value["run_id"],  # type: ignore[arg-type]
            owner_id=value["owner_id"],  # type: ignore[arg-type]
            expires_at=value["expires_at"],  # type: ignore[arg-type]
            fencing_token=value["fencing_token"],  # type: ignore[arg-type]
            created_at=value["created_at"],  # type: ignore[arg-type]
        )


class RunLeaseStore(Protocol):
    def acquire(
        self, run_id: str, owner_id: str, *, ttl_seconds: float = 30.0
    ) -> RunLease: ...

    def renew(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: float = 30.0,
    ) -> RunLease: ...

    def release(self, lease_id: str, owner_id: str, fencing_token: int) -> None: ...

    def get(self, lease_id: str) -> RunLease: ...


class InMemoryRunLeaseStore:
    """Thread-safe reference implementation for embedded and contract tests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._leases: dict[str, RunLease] = {}
        self._run_leases: dict[str, str] = {}
        self._next_token = 0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _ttl(ttl_seconds: float) -> float:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a number")
        if not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be greater than zero and at most one day")
        return float(ttl_seconds)

    @staticmethod
    def _token(fencing_token: int) -> int:
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
            raise ValueError("fencing_token must be an integer")
        return fencing_token

    def acquire(
        self, run_id: str, owner_id: str, *, ttl_seconds: float = 30.0
    ) -> RunLease:
        if not run_id or not owner_id:
            raise ValueError("run_id and owner_id are required")
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            now = self._now()
            current_id = self._run_leases.get(run_id)
            current = self._leases.get(current_id) if current_id else None
            if current is not None and current.is_active(now=now):
                raise LeaseConflict(
                    f"Run {run_id} is leased by another worker",
                    details={"run_id": run_id, "owner_id": current.owner_id},
                )
            self._next_token += 1
            lease = RunLease(
                lease_id=new_id("lease"),
                run_id=run_id,
                owner_id=owner_id,
                expires_at=(now + timedelta(seconds=ttl)).isoformat(),
                fencing_token=self._next_token,
                created_at=now.isoformat(),
            )
            self._leases[lease.lease_id] = lease
            self._run_leases[run_id] = lease.lease_id
            return lease

    def renew(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: float = 30.0,
    ) -> RunLease:
        ttl = self._ttl(ttl_seconds)
        token = self._token(fencing_token)
        with self._lock:
            current = self._leases.get(lease_id)
            if current is None:
                raise LeaseNotFound(lease_id)
            if (
                current.owner_id != owner_id
                or current.fencing_token != token
                or self._run_leases.get(current.run_id) != lease_id
                or not current.is_active(now=self._now())
            ):
                raise LeaseConflict("Lease owner or fencing token is stale")
            now = self._now()
            renewed = RunLease(
                lease_id=current.lease_id,
                run_id=current.run_id,
                owner_id=current.owner_id,
                expires_at=(now + timedelta(seconds=ttl)).isoformat(),
                fencing_token=current.fencing_token,
                created_at=current.created_at,
            )
            self._leases[lease_id] = renewed
            return renewed

    def release(self, lease_id: str, owner_id: str, fencing_token: int) -> None:
        token = self._token(fencing_token)
        with self._lock:
            current = self._leases.get(lease_id)
            if current is None:
                raise LeaseNotFound(lease_id)
            if (
                current.owner_id != owner_id
                or current.fencing_token != token
                or self._run_leases.get(current.run_id) != lease_id
                or not current.is_active(now=self._now())
            ):
                raise LeaseConflict("Lease owner or fencing token is stale")
            self._leases.pop(lease_id, None)
            self._run_leases.pop(current.run_id, None)

    def get(self, lease_id: str) -> RunLease:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise LeaseNotFound(lease_id)
            return lease
