"""Append-only event stores with optimistic concurrency."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiharness.core.errors import ConcurrencyConflict, EventConflict, SessionNotFound
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
