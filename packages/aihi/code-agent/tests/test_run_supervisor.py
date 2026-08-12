from __future__ import annotations

from threading import Event, Thread

import pytest
from aihi.agent import Event as AgentEvent
from aihi.code_agent.worker import RunConflict, RunSupervisor, WorkerServer


def test_supervisor_allows_one_run_per_session_and_releases_the_slot() -> None:
    supervisor = RunSupervisor(max_workers=2)
    release = Event()
    try:
        first = supervisor.submit(
            request_id=1,
            session_id="ses_one",
            run_id="run_one",
            target=lambda cancel: {
                "cancelled": cancel.wait(timeout=2),
                "released": release.is_set(),
            },
        )
        with pytest.raises(RunConflict, match="Session already has an active Run"):
            supervisor.submit(
                request_id=2,
                session_id="ses_one",
                run_id="run_two",
                target=lambda _cancel: {},
            )

        other = supervisor.submit(
            request_id=3,
            session_id="ses_two",
            run_id="run_other",
            target=lambda _cancel: {"ok": True},
        )
        assert other.future.result(timeout=2) == {"ok": True}
        assert supervisor.request_cancel(session_id="ses_one", run_id="run_one") is True
        assert first.future.result(timeout=2) == {"cancelled": True, "released": False}
        completed = supervisor.drain_completed()
        assert {item.run_id for item in completed} == {"run_one", "run_other"}

        replacement = supervisor.submit(
            request_id=4,
            session_id="ses_one",
            run_id="run_replacement",
            target=lambda _cancel: {"ok": True},
        )
        assert replacement.future.result(timeout=2) == {"ok": True}
    finally:
        release.set()
        supervisor.close()


def test_worker_notification_queue_does_not_lose_concurrent_events() -> None:
    server = WorkerServer()

    def emit(worker: int) -> None:
        for index in range(100):
            server._observe_event(
                AgentEvent(
                    type="model.chunk",
                    session_id=f"ses_{worker}",
                    run_id=f"run_{worker}",
                    data={"index": index},
                    ephemeral=True,
                )
            )

    threads = [Thread(target=emit, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    notifications = server.drain_notifications()
    assert len(notifications) == 400
    assert server.drain_notifications() == []
