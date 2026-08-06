from __future__ import annotations

from pathlib import Path

import pytest

from aiharness.core.errors import ProviderFailure
from aiharness.core.types import Message
from aiharness.models.providers.fake import FakeProvider, FakeStep
from aiharness.observability import (
    Telemetry,
    TelemetryError,
    TraceContext,
    W3CTracePropagator,
    WorkerTraceManager,
)
from aiharness.runtime import RunCoordinator, RunState
from aiharness.sandbox import HostBackend
from aiharness.sessions import InMemoryEventStore, Session
from aiharness.tools import ToolRegistry


class _FlushSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.flush_calls = 0
        self.fail = fail

    def record(self, _observation) -> None:
        pass

    def flush(self) -> None:
        self.flush_calls += 1
        if self.fail:
            raise RuntimeError("telemetry backend unavailable")


def _session(tmp_path: Path, session_id: str) -> Session:
    return Session.create(
        InMemoryEventStore(),
        cwd=tmp_path,
        provider="fake",
        model="fake-model",
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_runtime_flushes_shared_telemetry_after_success_and_failure(tmp_path: Path) -> None:
    success_sink = _FlushSink()
    success_telemetry = Telemetry(success_sink)
    success_session = _session(tmp_path, "ses-telemetry-success")
    success = RunCoordinator(
        FakeProvider([FakeStep(text="done")]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
        telemetry=success_telemetry,
    )
    result = await success.run(
        success_session, model="fake-model", user_message=Message.text("user", "hello")
    )
    assert result.state is RunState.COMPLETED
    assert success_sink.flush_calls == 1
    assert success_session.events[-1].type == "run.completed"

    failure_sink = _FlushSink()
    failure_telemetry = Telemetry(failure_sink)
    failure_session = _session(tmp_path, "ses-telemetry-failure")
    failure = RunCoordinator(
        FakeProvider([FakeStep(error=ProviderFailure("provider unavailable"))]),
        registry=ToolRegistry(),
        sandbox=HostBackend(tmp_path, unsafe=True),
        telemetry=failure_telemetry,
    )
    failed = await failure.run(failure_session, model="fake-model")
    assert failed.state is RunState.FAILED
    assert failure_sink.flush_calls == 1
    assert failure_session.events[-1].type == "run.failed"


def test_telemetry_flush_is_fail_open_and_worker_trace_refresh_is_child_span() -> None:
    telemetry = Telemetry(_FlushSink(fail=True))
    assert telemetry.flush() is False
    parent = telemetry.trace_for_run("run-parent")
    manager = WorkerTraceManager(telemetry)
    first = manager.start("worker-1", "run-parent")
    assert first.attempt == 1
    assert first.trace.trace_id == parent.trace_id
    assert first.trace.parent_span_id == parent.span_id
    assert first.headers()["traceparent"] == first.traceparent

    incoming_parent = TraceContext("a" * 32, "b" * 16, sampled=True)
    refreshed = manager.refresh_from_carrier(
        "worker-1", "run-parent", W3CTracePropagator.inject(incoming_parent)
    )
    assert refreshed.attempt == 2
    assert refreshed.trace.trace_id == incoming_parent.trace_id
    assert refreshed.trace.parent_span_id == incoming_parent.span_id
    assert refreshed.trace.span_id != first.trace.span_id
    with pytest.raises(TelemetryError):
        manager.refresh("worker-1", "another-run")
    with pytest.raises(TelemetryError):
        manager.refresh_from_carrier("worker-1", "run-parent", {})
    with pytest.raises(TelemetryError):
        manager.start("worker-2", "run-parent", parent_trace=False)  # type: ignore[arg-type]
