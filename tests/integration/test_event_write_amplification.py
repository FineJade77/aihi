"""Ephemeral stream deltas and batched durable writes."""

from pathlib import Path

import pytest

from aiharness.core.errors import EventInvariantViolation
from aiharness.core.events import Event
from aiharness.core.types import Message
from aiharness.models.providers.fake import FakeProvider, FakeStep
from aiharness.observability import InMemoryTelemetrySink, Telemetry
from aiharness.runtime import RunCoordinator, RunState
from aiharness.sandbox import HostBackend
from aiharness.sessions import InMemoryEventStore, Session
from aiharness.tools import ToolRegistry


class CountingEventStore(InMemoryEventStore):
    """Count store transactions so batching cannot silently regress."""

    def __init__(self) -> None:
        super().__init__()
        self.transactions = 0
        self.rows = 0

    def append(self, session_id: str, expected_seq: int, events: list[Event]) -> list[Event]:
        self.transactions += 1
        self.rows += len(events)
        return super().append(session_id, expected_seq, events)


def session_for(store: InMemoryEventStore, tmp_path: Path, name: str) -> Session:
    return Session.create(
        store, cwd=tmp_path, provider="fake", model="fake-model", session_id=name
    )


def test_emit_and_append_reject_the_wrong_durability(tmp_path: Path) -> None:
    session = session_for(InMemoryEventStore(), tmp_path, "ses-durability")
    durable = Event(type="user.message", session_id=session.id, data={})
    stream = Event(type="model.chunk", session_id=session.id, ephemeral=True)

    with pytest.raises(EventInvariantViolation):
        session.append(stream)
    with pytest.raises(EventInvariantViolation):
        session.emit(durable)

    observed: list[Event] = []
    session.add_event_observer(observed.append)
    head_before = session.head_seq
    session.emit(stream)

    assert [event.type for event in observed] == ["model.chunk"]
    assert session.head_seq == head_before
    assert not any(event.type == "model.chunk" for event in session.events)


@pytest.mark.asyncio
async def test_streaming_run_writes_no_chunk_rows(tmp_path: Path) -> None:
    store = CountingEventStore()
    session = session_for(store, tmp_path, "ses-stream")
    chunks: list[Event] = []
    session.add_event_observer(
        lambda event: chunks.append(event) if event.type == "model.chunk" else None
    )
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="x" * 2_000)]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )
    baseline = store.transactions

    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "hi")
    )

    assert result.state == RunState.COMPLETED
    # ~170 deltas reach the UI; none of them reaches the log.
    assert len(chunks) > 100
    assert all(event.ephemeral for event in chunks)
    assert not any(event.type == "model.chunk" for event in session.events)
    # A plain text run: message+start, state, assistant message, state+completed.
    assert store.transactions - baseline <= 4
    assert len(session.events) <= 8


@pytest.mark.asyncio
async def test_telemetry_ignores_ephemeral_stream_deltas(tmp_path: Path) -> None:
    sink = InMemoryTelemetrySink()
    telemetry = Telemetry(sink)
    session = session_for(InMemoryEventStore(), tmp_path, "ses-telemetry")
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="y" * 500)]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
        telemetry=telemetry,
    )

    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "hi"))

    names = [record.name for record in sink.records()]
    assert "model.chunk" not in names
    assert "run.completed" in names


@pytest.mark.asyncio
async def test_artifact_projection_does_not_rescan_history_each_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact set is built once per run, not re-derived from every event."""

    store = CountingEventStore()
    session = session_for(store, tmp_path, "ses-projection")
    coordinator = RunCoordinator(
        FakeProvider([FakeStep(text="one"), FakeStep(text="two")]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
    )
    scans = 0
    original = RunCoordinator._recorded_artifact_ids

    def counting(target: Session) -> set[str]:
        nonlocal scans
        scans += 1
        return original(target)

    monkeypatch.setattr(RunCoordinator, "_recorded_artifact_ids", staticmethod(counting))

    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "a"))
    await coordinator.run(session, model="fake-model", user_message=Message.text("user", "b"))

    # Once per run, regardless of how many model turns the run makes.
    assert scans == 2
