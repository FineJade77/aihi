"""Fenced, run-scoped worker leases.

Leases are control-plane state, not an authorization grant.  A worker must
still pass the normal policy/tool/sandbox chain for every side effect.  The
fencing token makes a stale worker unable to renew or release a lease after a
different worker has taken it over.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiharness.core.errors import LeaseConflict, LeaseNotFound, StoreUnavailable
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


class PostgresRunLeaseStore:
    """PostgreSQL RunLeaseStore with transactional fencing and takeover rows.

    The adapter only requires a small DB-API connection surface and accepts an
    injected connection for deterministic contract tests.  Expired leases are
    retained as non-current rows so stale owners receive a fencing conflict
    instead of silently acquiring authority after a takeover.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        connection_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if connection is not None and (dsn is not None or connection_factory is not None):
            raise ValueError("connection cannot be combined with dsn or connection_factory")
        if connection is None and dsn is None:
            raise ValueError("dsn is required when connection is not provided")
        if connection is None:
            if connection_factory is not None:
                if dsn is None:
                    raise ValueError("connection_factory requires a dsn")
                try:
                    connection = connection_factory(dsn)
                except Exception as exc:
                    raise StoreUnavailable("Unable to connect to PostgreSQL leases") from exc
            else:
                assert dsn is not None
                try:
                    import psycopg
                except ImportError as exc:  # pragma: no cover - optional deployment extra
                    raise StoreUnavailable(
                        "PostgreSQL support requires the optional psycopg package"
                    ) from exc
                try:
                    connection = psycopg.connect(dsn)
                except Exception as exc:  # pragma: no cover - requires external service
                    raise StoreUnavailable("Unable to connect to PostgreSQL leases") from exc
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        try:
            self._initialize()
        except Exception as exc:
            self._close_quietly()
            if isinstance(exc, StoreUnavailable):
                raise
            raise StoreUnavailable("Unable to initialize PostgreSQL leases") from exc

    def _cursor(self) -> Any:
        try:
            return self._connection.cursor()
        except Exception as exc:
            raise StoreUnavailable("PostgreSQL lease connection is unavailable") from exc

    @staticmethod
    def _close_cursor(cursor: Any) -> None:
        close = getattr(cursor, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_quietly(self) -> None:
        close = getattr(self._connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._close_quietly()

    def _rollback_quietly(self) -> None:
        rollback = getattr(self._connection, "rollback", None)
        if callable(rollback):
            try:
                rollback()
            except Exception:
                pass

    def _initialize(self) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                "CREATE SEQUENCE IF NOT EXISTS aiharness_run_lease_fencing_seq"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS run_leases (
                    lease_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    fencing_token BIGINT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    is_current BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_leases_current_run
                ON run_leases(run_id) WHERE is_current = TRUE
                """
            )
            self._connection.commit()
        except Exception:
            self._rollback_quietly()
            raise
        finally:
            self._close_cursor(cursor)

    @staticmethod
    def _row_value(row: Any, name: str, index: int) -> Any:
        if isinstance(row, Mapping):
            return row[name]
        return row[index]

    @classmethod
    def _lease_from_row(cls, row: Any) -> RunLease:
        return RunLease(
            lease_id=str(cls._row_value(row, "lease_id", 0)),
            run_id=str(cls._row_value(row, "run_id", 1)),
            owner_id=str(cls._row_value(row, "owner_id", 2)),
            expires_at=str(cls._row_value(row, "expires_at", 3)),
            fencing_token=int(cls._row_value(row, "fencing_token", 4)),
            created_at=str(cls._row_value(row, "created_at", 5)),
        )

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _is_unique_violation(exc: BaseException) -> bool:
        state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if state is not None:
            return state == "23505"
        return "unique" in type(exc).__name__.lower()

    def acquire(self, run_id: str, owner_id: str, *, ttl_seconds: float = 30.0) -> RunLease:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id is required")
        ttl = InMemoryRunLeaseStore._ttl(ttl_seconds)
        run_id = run_id.strip()
        owner_id = owner_id.strip()
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute(
                    """
                    SELECT lease_id, run_id, owner_id, expires_at, fencing_token, created_at
                    FROM run_leases
                    WHERE run_id = %s AND is_current = TRUE
                    FOR UPDATE
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
                now = self._now()
                if row is not None:
                    current = self._lease_from_row(row)
                    if current.is_active(now=now):
                        raise LeaseConflict(
                            f"Run {run_id} is leased by another worker",
                            details={"run_id": run_id, "owner_id": current.owner_id},
                        )
                    cursor.execute(
                        "UPDATE run_leases SET is_current = FALSE WHERE lease_id = %s",
                        (current.lease_id,),
                    )
                cursor.execute("SELECT nextval('aiharness_run_lease_fencing_seq')")
                token_row = cursor.fetchone()
                if token_row is None:
                    raise StoreUnavailable("PostgreSQL lease sequence returned no value")
                token = int(self._row_value(token_row, "nextval", 0))
                lease = RunLease(
                    lease_id=new_id("lease"),
                    run_id=run_id,
                    owner_id=owner_id,
                    expires_at=(now + timedelta(seconds=ttl)).isoformat(),
                    fencing_token=token,
                    created_at=now.isoformat(),
                )
                cursor.execute(
                    """
                    INSERT INTO run_leases(
                        lease_id, run_id, owner_id, expires_at, fencing_token,
                        created_at, is_current
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    """,
                    (
                        lease.lease_id,
                        lease.run_id,
                        lease.owner_id,
                        lease.expires_at,
                        lease.fencing_token,
                        lease.created_at,
                    ),
                )
                self._connection.commit()
                return lease
            except LeaseConflict:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                if self._is_unique_violation(exc):
                    raise LeaseConflict("Run lease was acquired concurrently") from exc
                if isinstance(exc, StoreUnavailable):
                    raise
                raise StoreUnavailable("Unable to acquire PostgreSQL lease") from exc
            finally:
                self._close_cursor(cursor)

    def _read_for_update(self, cursor: Any, lease_id: str) -> RunLease:
        cursor.execute(
            """
            SELECT lease_id, run_id, owner_id, expires_at, fencing_token, created_at
            FROM run_leases WHERE lease_id = %s FOR UPDATE
            """,
            (lease_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise LeaseNotFound(lease_id)
        return self._lease_from_row(row)

    def _is_current(self, cursor: Any, lease: RunLease) -> bool:
        cursor.execute(
            "SELECT lease_id FROM run_leases WHERE run_id = %s AND is_current = TRUE",
            (lease.run_id,),
        )
        row = cursor.fetchone()
        return row is not None and str(self._row_value(row, "lease_id", 0)) == lease.lease_id

    def renew(
        self,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: float = 30.0,
    ) -> RunLease:
        ttl = InMemoryRunLeaseStore._ttl(ttl_seconds)
        token = InMemoryRunLeaseStore._token(fencing_token)
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute("BEGIN")
                current = self._read_for_update(cursor, lease_id)
                if (
                    current.owner_id != owner_id
                    or current.fencing_token != token
                    or not self._is_current(cursor, current)
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
                cursor.execute(
                    "UPDATE run_leases SET expires_at = %s WHERE lease_id = %s",
                    (renewed.expires_at, renewed.lease_id),
                )
                self._connection.commit()
                return renewed
            except (LeaseConflict, LeaseNotFound):
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to renew PostgreSQL lease") from exc
            finally:
                self._close_cursor(cursor)

    def release(self, lease_id: str, owner_id: str, fencing_token: int) -> None:
        token = InMemoryRunLeaseStore._token(fencing_token)
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute("BEGIN")
                current = self._read_for_update(cursor, lease_id)
                if (
                    current.owner_id != owner_id
                    or current.fencing_token != token
                    or not self._is_current(cursor, current)
                    or not current.is_active(now=self._now())
                ):
                    raise LeaseConflict("Lease owner or fencing token is stale")
                cursor.execute(
                    "UPDATE run_leases SET is_current = FALSE WHERE lease_id = %s",
                    (lease_id,),
                )
                self._connection.commit()
            except (LeaseConflict, LeaseNotFound):
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to release PostgreSQL lease") from exc
            finally:
                self._close_cursor(cursor)

    def get(self, lease_id: str) -> RunLease:
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(
                    """
                    SELECT lease_id, run_id, owner_id, expires_at, fencing_token, created_at
                    FROM run_leases WHERE lease_id = %s
                    """,
                    (lease_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise LeaseNotFound(lease_id)
                lease = self._lease_from_row(row)
                self._connection.commit()
                return lease
            except LeaseNotFound:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to load PostgreSQL lease") from exc
            finally:
                self._close_cursor(cursor)
