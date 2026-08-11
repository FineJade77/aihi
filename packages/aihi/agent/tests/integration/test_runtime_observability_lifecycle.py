from __future__ import annotations

from pathlib import Path

import pytest
from aihi.agent.observability import Telemetry
from aihi.agent.runtime import RunCoordinator, RunState
from aihi.agent.sandbox import HostBackend
from aihi.agent.sessions import InMemoryEventStore, Session
from aihi.agent.tools import ToolRegistry
from aihi.models import FakeProvider, FakeStep, Message, ProviderFailure


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


def test_telemetry_flush_is_fail_open() -> None:
    """A failing sink is an observability problem, never a run problem."""

    telemetry = Telemetry(_FlushSink(fail=True))

    assert telemetry.flush() is False
