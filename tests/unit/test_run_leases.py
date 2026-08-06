from datetime import UTC, datetime, timedelta

import pytest

from aiharness.core.errors import LeaseConflict, LeaseNotFound
from aiharness.sessions.leases import InMemoryRunLeaseStore, RunLease


def test_lease_round_trip_and_fencing() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRunLeaseStore(clock=lambda: now)
    lease = store.acquire("run-1", "worker-a", ttl_seconds=10)
    assert RunLease.from_dict(lease.to_dict()) == lease
    with pytest.raises(LeaseConflict):
        store.acquire("run-1", "worker-b")
    renewed = store.renew(lease.lease_id, "worker-a", lease.fencing_token, ttl_seconds=20)
    assert renewed.expires_at == (now + timedelta(seconds=20)).isoformat()
    with pytest.raises(LeaseConflict):
        store.renew(lease.lease_id, "worker-b", lease.fencing_token)


def test_expired_lease_can_be_taken_over_but_stale_owner_cannot_release() -> None:
    clock_now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRunLeaseStore(clock=lambda: clock_now)
    first = store.acquire("run-1", "worker-a", ttl_seconds=1)
    clock_now = clock_now + timedelta(seconds=2)
    second = store.acquire("run-1", "worker-b", ttl_seconds=10)
    assert second.fencing_token > first.fencing_token
    with pytest.raises(LeaseConflict):
        store.release(first.lease_id, "worker-a", first.fencing_token)
    store.release(second.lease_id, "worker-b", second.fencing_token)
    with pytest.raises(LeaseNotFound):
        store.get(second.lease_id)


def test_expired_lease_cannot_be_released_before_takeover() -> None:
    clock_now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryRunLeaseStore(clock=lambda: clock_now)
    lease = store.acquire("run-1", "worker-a", ttl_seconds=1)
    clock_now = clock_now + timedelta(seconds=2)
    with pytest.raises(LeaseConflict):
        store.release(lease.lease_id, "worker-a", lease.fencing_token)


def test_invalid_ttl_is_rejected() -> None:
    with pytest.raises(ValueError):
        InMemoryRunLeaseStore().acquire("run", "worker", ttl_seconds=0)
