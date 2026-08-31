from __future__ import annotations

from aihi.agent import (
    InMemoryEventStore,
    RuntimeBuilder,
    Session,
)
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import ReadTestTool


async def test_a_run_persists_usage_and_context_size(tmp_path) -> None:
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = RuntimeBuilder(
        provider=FakeProvider([FakeStep(text="done")]),
        model="demo",
        tools=[ReadTestTool(tmp_path)],
    ).build()

    await runtime.coordinator.run(
        session, model="demo", user_message=Message.text("user", "hi")
    )

    usage_events = [e for e in session.events if e.type == "model.usage"]
    assert usage_events, "a completed run must record what it spent"
    data = usage_events[-1].data
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "context_tokens",
        "context_limit",
        "model",
    ):
        assert key in data, key
    assert data["context_limit"] > 0
    assert data["context_tokens"] > 0
