from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiharness.core.errors import LeaseConflict, LeaseNotFound, StoreUnavailable
from aiharness.sessions import PostgresRunLeaseStore


class _DbError(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _LeaseCursor:
    def __init__(self, connection: _LeaseConnection) -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    @staticmethod
    def _row(lease: dict[str, object]) -> tuple[object, ...]:
        return (
            lease["lease_id"],
            lease["run_id"],
            lease["owner_id"],
            lease["expires_at"],
            lease["fencing_token"],
            lease["created_at"],
        )

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(sql.split()).upper()
        self.rows = []
        if normalized.startswith("CREATE SEQUENCE"):
            return
        if normalized.startswith("CREATE TABLE") or normalized.startswith("CREATE UNIQUE INDEX"):
            return
        if normalized in {"BEGIN", "COMMIT", "ROLLBACK"}:
            return
        if normalized.startswith("SELECT LEASE_ID, RUN_ID") and "WHERE RUN_ID" in normalized:
            run_id = str(params[0])
            for lease in self.connection.leases.values():
                if lease["run_id"] == run_id and lease["is_current"]:
                    self.rows = [self._row(lease)]
                    break
            return
        if normalized.startswith("SELECT NEXTVAL"):
            self.connection.sequence += 1
            self.rows = [(self.connection.sequence,)]
            return
        if normalized.startswith("INSERT INTO RUN_LEASES"):
            lease_id, run_id, owner_id, expires_at, token, created_at = params
            if any(
                lease["is_current"] and lease["run_id"] == run_id
                for lease in self.connection.leases.values()
            ):
                raise _DbError("duplicate current run", "23505")
            self.connection.leases[str(lease_id)] = {
                "lease_id": str(lease_id),
                "run_id": str(run_id),
                "owner_id": str(owner_id),
                "expires_at": str(expires_at),
                "fencing_token": int(token),
                "created_at": str(created_at),
                "is_current": True,
            }
            return
        if normalized.startswith("UPDATE RUN_LEASES SET IS_CURRENT = FALSE"):
            lease = self.connection.leases[str(params[0])]
            lease["is_current"] = False
            return
        if normalized.startswith("SELECT LEASE_ID, RUN_ID") and "WHERE LEASE_ID" in normalized:
            lease = self.connection.leases.get(str(params[0]))
            if lease is not None:
                self.rows = [self._row(lease)]
            return
        if normalized.startswith("SELECT LEASE_ID FROM RUN_LEASES"):
            run_id = str(params[0])
            for lease in self.connection.leases.values():
                if lease["run_id"] == run_id and lease["is_current"]:
                    self.rows = [(lease["lease_id"],)]
                    break
            return
        if normalized.startswith("UPDATE RUN_LEASES SET EXPIRES_AT"):
            expires_at, lease_id = params
            self.connection.leases[str(lease_id)]["expires_at"] = str(expires_at)
            return
        raise AssertionError(f"unhandled SQL: {sql}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        pass


class _LeaseConnection:
    def __init__(self) -> None:
        self.leases: dict[str, dict[str, object]] = {}
        self.sequence = 0
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> _LeaseCursor:
        return _LeaseCursor(self)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        pass


def test_postgres_lease_store_takeover_fencing_and_persistence() -> None:
    now = [datetime(2026, 8, 6, tzinfo=UTC)]
    connection = _LeaseConnection()
    store = PostgresRunLeaseStore(connection=connection, clock=lambda: now[0])

    first = store.acquire("run-pg", "worker-a", ttl_seconds=1)
    renewed = store.renew(first.lease_id, first.owner_id, first.fencing_token, ttl_seconds=3)
    assert renewed.expires_at != first.expires_at
    assert store.get(first.lease_id) == renewed

    now[0] += timedelta(seconds=4)
    takeover = store.acquire("run-pg", "worker-b", ttl_seconds=30)
    assert takeover.fencing_token > first.fencing_token
    with pytest.raises(LeaseConflict):
        store.renew(first.lease_id, first.owner_id, first.fencing_token)
    with pytest.raises(LeaseConflict):
        store.release(first.lease_id, first.owner_id, first.fencing_token)
    store.release(takeover.lease_id, takeover.owner_id, takeover.fencing_token)
    assert connection.commit_count >= 5


def test_postgres_lease_store_maps_missing_and_connection_failures() -> None:
    store = PostgresRunLeaseStore(connection=_LeaseConnection())
    with pytest.raises(LeaseNotFound):
        store.get("missing")

    def broken(_: str) -> object:
        raise OSError("offline")

    with pytest.raises(StoreUnavailable):
        PostgresRunLeaseStore("postgresql://unused", connection_factory=broken)
