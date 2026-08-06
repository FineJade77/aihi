"""Append-only event stores with optimistic concurrency."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiharness.core.errors import (
    ConcurrencyConflict,
    EventConflict,
    SessionNotFound,
    StoreUnavailable,
)
from aiharness.core.events import Event, utc_now


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    head_seq: int
    created_at: str
    metadata: dict[str, Any]
    parent_session_id: str | None = None


class EventStore(Protocol):
    def create_session(
        self,
        session_id: str,
        metadata: dict[str, Any],
        *,
        parent_session_id: str | None = None,
    ) -> None: ...

    def append(self, session_id: str, expected_seq: int, events: list[Event]) -> list[Event]: ...

    def read(self, session_id: str, *, after_seq: int = 0) -> list[Event]: ...

    def get(self, session_id: str) -> SessionInfo: ...

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]: ...


class InMemoryEventStore:
    """Reference store used by unit tests and embedded ephemeral runs."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._events: dict[str, list[Event]] = {}
        self._event_sessions: dict[str, str] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        session_id: str,
        metadata: dict[str, Any],
        *,
        parent_session_id: str | None = None,
    ) -> None:
        with self._lock:
            if session_id in self._sessions:
                raise ConcurrencyConflict(f"Session already exists: {session_id}")
            self._sessions[session_id] = SessionInfo(
                session_id=session_id,
                head_seq=0,
                created_at=utc_now(),
                metadata=dict(metadata),
                parent_session_id=parent_session_id,
            )
            self._events[session_id] = []

    def append(self, session_id: str, expected_seq: int, events: list[Event]) -> list[Event]:
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                raise SessionNotFound(session_id)
            if info.head_seq != expected_seq:
                raise ConcurrencyConflict(
                    f"Expected session {session_id} at seq {expected_seq}, found {info.head_seq}",
                    details={"expected_seq": expected_seq, "actual_seq": info.head_seq},
                )
            event_ids = [event.id for event in events]
            if len(set(event_ids)) != len(event_ids):
                raise EventConflict("An append batch contains duplicate event ids")
            persisted: list[Event] = []
            for offset, event in enumerate(events, start=1):
                if event.session_id != session_id:
                    raise ValueError("Cannot append an event to a different session")
                if event.id in self._event_sessions:
                    raise EventConflict(f"Event already exists: {event.id}")
                persisted.append(event.persisted(expected_seq + offset))
            self._events[session_id].extend(persisted)
            for event in persisted:
                self._event_sessions[event.id] = session_id
            self._sessions[session_id] = SessionInfo(
                session_id=info.session_id,
                head_seq=expected_seq + len(persisted),
                created_at=info.created_at,
                metadata=info.metadata,
                parent_session_id=info.parent_session_id,
            )
            return persisted

    def read(self, session_id: str, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFound(session_id)
            return [event for event in self._events[session_id] if (event.seq or 0) > after_seq]

    def get(self, session_id: str) -> SessionInfo:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as exc:
                raise SessionNotFound(session_id) from exc

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        with self._lock:
            values = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
            return values[:limit]


class SQLiteEventStore:
    """Single-file local event store mirroring the future PostgreSQL contract."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    head_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    parent_session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_session(
        self,
        session_id: str,
        metadata: dict[str, Any],
        *,
        parent_session_id: str | None = None,
    ) -> None:
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id, head_seq, created_at, metadata_json, parent_session_id
                    ) VALUES (?, 0, ?, ?, ?)
                    """,
                    (session_id, utc_now(), payload, parent_session_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflict(f"Session already exists: {session_id}") from exc

    def append(self, session_id: str, expected_seq: int, events: list[Event]) -> list[Event]:
        with self._lock:
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT head_seq FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                actual_seq = int(row["head_seq"])
                if actual_seq != expected_seq:
                    raise ConcurrencyConflict(
                        f"Expected session {session_id} at seq {expected_seq}, found {actual_seq}",
                        details={"expected_seq": expected_seq, "actual_seq": actual_seq},
                    )
                event_ids = [event.id for event in events]
                if len(set(event_ids)) != len(event_ids):
                    raise EventConflict("An append batch contains duplicate event ids")
                for event_id in event_ids:
                    if connection.execute(
                        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone() is not None:
                        raise EventConflict(f"Event already exists: {event_id}")
                persisted: list[Event] = []
                for offset, event in enumerate(events, start=1):
                    if event.session_id != session_id:
                        raise ValueError("Cannot append an event to a different session")
                    saved = event.persisted(expected_seq + offset)
                    connection.execute(
                        """
                        INSERT INTO events(
                            session_id, seq, event_id, event_type, run_id,
                            created_at, schema_version, data_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            saved.session_id,
                            saved.seq,
                            saved.id,
                            saved.type,
                            saved.run_id,
                            saved.created_at,
                            saved.schema_version,
                            json.dumps(saved.data, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                    persisted.append(saved)
                connection.execute(
                    "UPDATE sessions SET head_seq = ? WHERE session_id = ?",
                    (expected_seq + len(persisted), session_id),
                )
                connection.execute("COMMIT")
                return persisted
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def read(self, session_id: str, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            if self._connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone() is None:
                raise SessionNotFound(session_id)
            rows = self._connection.execute(
                """
                SELECT seq, event_id, event_type, run_id, created_at, schema_version, data_json
                FROM events WHERE session_id = ? AND seq > ? ORDER BY seq
                """,
                (session_id, after_seq),
            ).fetchall()
        return [
            Event(
                id=str(row["event_id"]),
                type=str(row["event_type"]),
                session_id=session_id,
                run_id=str(row["run_id"]) if row["run_id"] is not None else None,
                seq=int(row["seq"]),
                created_at=str(row["created_at"]),
                schema_version=int(row["schema_version"]),
                data=json.loads(str(row["data_json"])),
            )
            for row in rows
        ]

    def get(self, session_id: str) -> SessionInfo:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT session_id, head_seq, created_at, metadata_json, parent_session_id
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return self._info_from_row(row)

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT session_id, head_seq, created_at, metadata_json, parent_session_id
                FROM sessions ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._info_from_row(row) for row in rows]

    @staticmethod
    def _info_from_row(row: sqlite3.Row) -> SessionInfo:
        return SessionInfo(
            session_id=str(row["session_id"]),
            head_seq=int(row["head_seq"]),
            created_at=str(row["created_at"]),
            metadata=json.loads(str(row["metadata_json"])),
            parent_session_id=(
                str(row["parent_session_id"]) if row["parent_session_id"] is not None else None
            ),
        )


def _json_dump(value: object, *, field: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc


class PostgresEventStore:
    """PostgreSQL EventStore adapter using an injected DB-API connection.

    The adapter intentionally depends on the small DB-API surface (cursor,
    execute, fetch, commit, rollback) rather than importing a PostgreSQL
    driver in the core package.  Production callers can pass a connection
    factory, or a DSN when ``psycopg`` is installed.  Tests can provide a
    deterministic fake connection without a running database.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        connection_factory: Callable[[str], Any] | None = None,
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
                    raise StoreUnavailable("Unable to connect to PostgreSQL") from exc
            else:
                assert dsn is not None
                try:
                    import psycopg
                except ImportError as exc:  # pragma: no cover - depends on deployment extras
                    raise StoreUnavailable(
                        "PostgreSQL support requires the optional psycopg package"
                    ) from exc
                try:
                    connection = psycopg.connect(dsn)
                except Exception as exc:  # pragma: no cover - requires external service
                    raise StoreUnavailable("Unable to connect to PostgreSQL") from exc
        self._connection = connection
        self._lock = threading.RLock()
        try:
            self._initialize()
        except Exception as exc:
            self._close_quietly()
            if isinstance(exc, StoreUnavailable):
                raise
            raise StoreUnavailable("Unable to initialize PostgreSQL EventStore") from exc

    def _cursor(self) -> Any:
        try:
            return self._connection.cursor()
        except Exception as exc:
            raise StoreUnavailable("PostgreSQL connection is unavailable") from exc

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

    def _initialize(self) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    head_seq BIGINT NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    metadata_json JSONB NOT NULL,
                    parent_session_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    seq BIGINT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    data_json JSONB NOT NULL,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id)")
            self._connection.commit()
        except Exception:
            self._rollback_quietly()
            raise
        finally:
            self._close_cursor(cursor)

    def _rollback_quietly(self) -> None:
        rollback = getattr(self._connection, "rollback", None)
        if callable(rollback):
            try:
                rollback()
            except Exception:
                pass

    @staticmethod
    def _is_unique_violation(exc: BaseException) -> bool:
        state: object = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if state is not None:
            return state == "23505"
        name = type(exc).__name__.lower()
        return "unique" in name

    @staticmethod
    def _row_value(row: Any, name: str, index: int) -> Any:
        if isinstance(row, Mapping):
            return row[name]
        return row[index]

    @classmethod
    def _info_from_row(cls, row: Any) -> SessionInfo:
        metadata = cls._row_value(row, "metadata_json", 3)
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            raise StoreUnavailable("PostgreSQL session metadata is not a JSON object")
        parent = cls._row_value(row, "parent_session_id", 4)
        return SessionInfo(
            session_id=str(cls._row_value(row, "session_id", 0)),
            head_seq=int(cls._row_value(row, "head_seq", 1)),
            created_at=str(cls._row_value(row, "created_at", 2)),
            metadata=dict(metadata),
            parent_session_id=str(parent) if parent is not None else None,
        )

    @classmethod
    def _event_from_row(cls, row: Any, session_id: str) -> Event:
        payload = cls._row_value(row, "data_json", 6)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise StoreUnavailable("PostgreSQL event data is not a JSON object")
        run_id = cls._row_value(row, "run_id", 3)
        return Event(
            id=str(cls._row_value(row, "event_id", 1)),
            type=str(cls._row_value(row, "event_type", 2)),
            session_id=session_id,
            run_id=str(run_id) if run_id is not None else None,
            seq=int(cls._row_value(row, "seq", 0)),
            created_at=str(cls._row_value(row, "created_at", 4)),
            schema_version=int(cls._row_value(row, "schema_version", 5)),
            data=dict(payload),
        )

    def create_session(
        self,
        session_id: str,
        metadata: dict[str, Any],
        *,
        parent_session_id: str | None = None,
    ) -> None:
        metadata_json = _json_dump(metadata, field="metadata")
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(
                    "SELECT 1 FROM sessions WHERE session_id = %s", (session_id,)
                )
                if cursor.fetchone() is not None:
                    raise ConcurrencyConflict(f"Session already exists: {session_id}")
                cursor.execute(
                    """
                    INSERT INTO sessions(
                        session_id, head_seq, created_at, metadata_json, parent_session_id
                    ) VALUES (%s, 0, %s, %s::jsonb, %s)
                    """,
                    (
                        session_id,
                        utc_now(),
                        metadata_json,
                        parent_session_id,
                    ),
                )
                self._connection.commit()
            except ConcurrencyConflict:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                if self._is_unique_violation(exc):
                    raise ConcurrencyConflict(f"Session already exists: {session_id}") from exc
                raise StoreUnavailable("Unable to create PostgreSQL session") from exc
            finally:
                self._close_cursor(cursor)

    def append(self, session_id: str, expected_seq: int, events: list[Event]) -> list[Event]:
        event_ids = [event.id for event in events]
        if len(set(event_ids)) != len(event_ids):
            raise EventConflict("An append batch contains duplicate event ids")
        if any(event.session_id != session_id for event in events):
            raise ValueError("Cannot append an event to a different session")
        event_json = {
            event.id: _json_dump(event.data, field="event data") for event in events
        }
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute(
                    "SELECT head_seq FROM sessions WHERE session_id = %s FOR UPDATE",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                actual_seq = int(self._row_value(row, "head_seq", 0))
                if actual_seq != expected_seq:
                    raise ConcurrencyConflict(
                        f"Expected session {session_id} at seq {expected_seq}, found {actual_seq}",
                        details={"expected_seq": expected_seq, "actual_seq": actual_seq},
                    )
                persisted: list[Event] = []
                for offset, event in enumerate(events, start=1):
                    cursor.execute("SELECT 1 FROM events WHERE event_id = %s", (event.id,))
                    if cursor.fetchone() is not None:
                        raise EventConflict(f"Event already exists: {event.id}")
                    saved = event.persisted(expected_seq + offset)
                    cursor.execute(
                        """
                        INSERT INTO events(
                            session_id, seq, event_id, event_type, run_id,
                            created_at, schema_version, data_json
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            saved.session_id,
                            saved.seq,
                            saved.id,
                            saved.type,
                            saved.run_id,
                            saved.created_at,
                            saved.schema_version,
                            event_json[event.id],
                        ),
                    )
                    persisted.append(saved)
                cursor.execute(
                    "UPDATE sessions SET head_seq = %s WHERE session_id = %s",
                    (expected_seq + len(persisted), session_id),
                )
                self._connection.commit()
                return persisted
            except (ConcurrencyConflict, EventConflict, SessionNotFound):
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                if self._is_unique_violation(exc):
                    raise EventConflict("Event already exists") from exc
                raise StoreUnavailable("Unable to append PostgreSQL events") from exc
            finally:
                self._close_cursor(cursor)

    def read(self, session_id: str, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute("SELECT 1 FROM sessions WHERE session_id = %s", (session_id,))
                if cursor.fetchone() is None:
                    raise SessionNotFound(session_id)
                cursor.execute(
                    """
                    SELECT seq, event_id, event_type, run_id, created_at,
                           schema_version, data_json
                    FROM events WHERE session_id = %s AND seq > %s ORDER BY seq
                    """,
                    (session_id, after_seq),
                )
                events = [self._event_from_row(row, session_id) for row in cursor.fetchall()]
                self._connection.commit()
                return events
            except SessionNotFound:
                self._rollback_quietly()
                raise
            except StoreUnavailable:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to read PostgreSQL events") from exc
            finally:
                self._close_cursor(cursor)

    def get(self, session_id: str) -> SessionInfo:
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(
                    """
                    SELECT session_id, head_seq, created_at, metadata_json, parent_session_id
                    FROM sessions WHERE session_id = %s
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                info = self._info_from_row(row)
                self._connection.commit()
                return info
            except SessionNotFound:
                self._rollback_quietly()
                raise
            except StoreUnavailable:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to load PostgreSQL session") from exc
            finally:
                self._close_cursor(cursor)

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(
                    """
                    SELECT session_id, head_seq, created_at, metadata_json, parent_session_id
                    FROM sessions ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
                sessions = [self._info_from_row(row) for row in cursor.fetchall()]
                self._connection.commit()
                return sessions
            except StoreUnavailable:
                self._rollback_quietly()
                raise
            except Exception as exc:
                self._rollback_quietly()
                raise StoreUnavailable("Unable to list PostgreSQL sessions") from exc
            finally:
                self._close_cursor(cursor)
