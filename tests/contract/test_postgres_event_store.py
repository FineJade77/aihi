import json

import pytest

from aiharness.core.errors import ConcurrencyConflict, EventConflict, StoreUnavailable
from aiharness.core.events import Event
from aiharness.sessions import PostgresEventStore, Session


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(sql.split()).upper()
        self.rows = []
        if normalized.startswith("CREATE TABLE") or normalized.startswith("CREATE INDEX"):
            return
        if normalized in {"BEGIN", "COMMIT", "ROLLBACK"}:
            return
        if normalized.startswith("SELECT 1 FROM SESSIONS"):
            sid = str(params[0])
            if sid in self.connection.sessions:
                self.rows = [(1,)]
            return
        if normalized.startswith("SELECT HEAD_SEQ FROM SESSIONS"):
            sid = str(params[0])
            if sid in self.connection.sessions:
                self.rows = [(self.connection.sessions[sid]["head_seq"],)]
            return
        if normalized.startswith("SELECT 1 FROM EVENTS"):
            event_id = str(params[0])
            if event_id in self.connection.event_ids:
                self.rows = [(1,)]
            return
        if normalized.startswith("INSERT INTO SESSIONS"):
            sid, created_at, metadata_json, parent = params
            self.connection.sessions[str(sid)] = {
                "head_seq": 0,
                "created_at": str(created_at),
                "metadata_json": json.loads(str(metadata_json)),
                "parent": parent,
            }
            return
        if normalized.startswith("INSERT INTO EVENTS"):
            if self.connection.raise_unique_on_event_insert:
                error = _DbError("duplicate key", "23505")
                raise error
            if self.connection.raise_nonunique_integrity:
                error = _DbError("foreign key violation", "23503")
                raise error
            sid, seq, event_id, event_type, run_id, created_at, schema_version, data_json = params
            event = (
                int(seq),
                str(event_id),
                str(event_type),
                run_id,
                str(created_at),
                int(schema_version),
                json.loads(str(data_json)),
            )
            self.connection.events.setdefault(str(sid), []).append(event)
            self.connection.event_ids.add(str(event_id))
            return
        if normalized.startswith("UPDATE SESSIONS SET HEAD_SEQ"):
            head_seq, sid = params
            self.connection.sessions[str(sid)]["head_seq"] = int(head_seq)
            return
        if normalized.startswith("SELECT SEQ, EVENT_ID"):
            sid, after_seq = str(params[0]), int(params[1])
            self.rows = [
                event for event in self.connection.events.get(sid, []) if event[0] > after_seq
            ]
            return
        if normalized.startswith("SELECT SESSION_ID, HEAD_SEQ"):
            if "LIMIT" in normalized:
                values = sorted(
                    self.connection.sessions.items(),
                    key=lambda item: item[1]["created_at"],
                    reverse=True,
                )[: int(params[0])]
            else:
                values = [(str(params[0]), self.connection.sessions[str(params[0])])]
            self.rows = [
                (
                    sid,
                    value["head_seq"],
                    value["created_at"],
                    value["metadata_json"],
                    value["parent"],
                )
                for sid, value in values
            ]
            return
        raise AssertionError(f"unhandled SQL: {sql}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, object]] = {}
        self.events: dict[str, list[tuple[object, ...]]] = {}
        self.event_ids: set[str] = set()
        self.raise_unique_on_event_insert = False
        self.raise_nonunique_integrity = False
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        pass


class _DbError(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _BrokenCursorConnection(_FakeConnection):
    def cursor(self) -> _FakeCursor:
        raise OSError("connection is closed")


class _CloseErrorCursor(_FakeCursor):
    def close(self) -> None:
        raise OSError("cursor close failed")


class _CloseErrorConnection(_FakeConnection):
    def cursor(self) -> _CloseErrorCursor:
        return _CloseErrorCursor(self)


def test_postgres_adapter_matches_event_store_contract() -> None:
    store = PostgresEventStore(connection=_FakeConnection())
    store.create_session("ses-pg", {"cwd": "/tmp", "provider": "fake"})
    event = Event(type="user.message", session_id="ses-pg", data={"text": "hello"})
    persisted = store.append("ses-pg", 0, [event])
    assert persisted[0].seq == 1
    assert store.get("ses-pg").head_seq == 1
    assert store.read("ses-pg")[0].to_dict() == persisted[0].to_dict()
    assert store.list_sessions(limit=1)[0].session_id == "ses-pg"
    with pytest.raises(ConcurrencyConflict):
        store.append("ses-pg", 0, [Event(type="assistant.message", session_id="ses-pg")])


def test_session_can_load_from_postgres_adapter() -> None:
    store = PostgresEventStore(connection=_FakeConnection())
    session = Session.create(
        store,
        cwd="/tmp",
        provider="fake",
        model="test",
        session_id="ses-load-pg",
    )
    loaded = Session.load(store, session.id)
    assert loaded.head_seq == session.head_seq
    assert loaded.metadata == session.metadata


def test_postgres_adapter_closes_read_transactions_and_maps_unique_races() -> None:
    connection = _FakeConnection()
    store = PostgresEventStore(connection=connection)
    store.create_session("ses-tx", {})
    store.get("ses-tx")
    store.read("ses-tx")
    store.list_sessions()
    assert connection.commit_count >= 4
    connection.raise_unique_on_event_insert = True
    with pytest.raises(EventConflict):
        store.append("ses-tx", 0, [Event(type="user.message", session_id="ses-tx")])
    assert connection.rollback_count >= 1


def test_postgres_adapter_rejects_driver_and_non_json_failures() -> None:
    def broken_factory(_: str) -> object:
        raise OSError("database offline")

    with pytest.raises(StoreUnavailable):
        PostgresEventStore("postgresql://unused", connection_factory=broken_factory)
    store = PostgresEventStore(connection=_FakeConnection())
    with pytest.raises(ValueError, match="JSON serializable"):
        store.create_session("ses-nan", {"value": float("nan")})


def test_postgres_adapter_closes_failed_connection_and_ignores_cursor_close_errors() -> None:
    with pytest.raises(StoreUnavailable):
        PostgresEventStore(connection=_BrokenCursorConnection())
    store = PostgresEventStore(connection=_CloseErrorConnection())
    store.create_session("ses-close", {})
    assert store.get("ses-close").session_id == "ses-close"


def test_postgres_adapter_does_not_misclassify_nonunique_integrity_errors() -> None:
    connection = _FakeConnection()
    store = PostgresEventStore(connection=connection)
    store.create_session("ses-integrity", {})
    connection.raise_nonunique_integrity = True
    with pytest.raises(StoreUnavailable):
        store.append("ses-integrity", 0, [Event(type="user.message", session_id="ses-integrity")])
