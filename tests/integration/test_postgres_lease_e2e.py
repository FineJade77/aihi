from __future__ import annotations

import os
from uuid import uuid4

import pytest

from aiharness.sessions import PostgresRunLeaseStore


def test_postgres_lease_store_live_e2e() -> None:
    dsn = os.getenv("AIHARNESS_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIHARNESS_POSTGRES_DSN is not configured")
    store = PostgresRunLeaseStore(dsn)
    run_id = f"e2e-{uuid4().hex}"
    try:
        lease = store.acquire(run_id, "worker-e2e", ttl_seconds=30)
        renewed = store.renew(lease.lease_id, lease.owner_id, lease.fencing_token, ttl_seconds=30)
        assert renewed.run_id == run_id
        assert renewed.fencing_token == lease.fencing_token
        store.release(renewed.lease_id, renewed.owner_id, renewed.fencing_token)
    finally:
        store.close()
