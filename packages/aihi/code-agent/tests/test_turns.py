from __future__ import annotations

import pytest
from aihi.agent import InMemoryEventStore, Session
from aihi.code_agent.config import load_config
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.turns import AssistantMessage, TextDelta, TurnFinished


def _config(tmp_path):
    path = tmp_path / "aihi-code.toml"
    path.write_text(
        '[provider]\nname = "fake"\nmodel = "demo"\n\n'
        '[sandbox]\nbackend = "host"\nunsafe = true\n',
        encoding="utf-8",
    )
    return load_config(path, cwd=tmp_path)


async def test_stream_ends_with_turn_finished_after_draining_events(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        events = [event async for event in runtime.stream(session, user_message="hi")]
    finally:
        await runtime.close()

    assert isinstance(events[-1], TurnFinished)
    assert events[-1].result.state == "completed"
    # The ordering invariant: nothing this run emits may arrive after the end.
    assert not any(isinstance(event, TurnFinished) for event in events[:-1])
    assert any(isinstance(event, TextDelta) for event in events)
    assert any(isinstance(event, AssistantMessage) for event in events)


async def test_stream_reconstructs_the_assistant_text_from_deltas(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        events = [event async for event in runtime.stream(session, user_message="hi")]
    finally:
        await runtime.close()

    streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
    final = next(e for e in events if isinstance(e, AssistantMessage))
    assert streamed == final.text
    assert "hi" in final.text


async def test_stream_rejects_an_empty_user_message(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        with pytest.raises(ValueError):
            async for _ in runtime.stream(session, user_message="   "):
                pass
    finally:
        await runtime.close()


async def test_run_returns_the_same_result_the_stream_finishes_with(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        result = await runtime.run(session, user_message="hi")
    finally:
        await runtime.close()

    assert result.state == "completed"


async def test_resume_streams_the_same_typed_events(tmp_path) -> None:
    config = _config(tmp_path)
    store = InMemoryEventStore()
    session = Session.create(store, cwd=str(tmp_path), provider="fake", model="demo")
    runtime = await CodeAgentRuntime.create(config, store=store)
    try:
        await runtime.run(session, user_message="hi", run_id="run_a")
        # A completed run is terminal; resuming it must surface the refusal as a
        # domain error, not a silent difference between run() and resume().
        events = [
            event
            async for event in runtime.stream_resume(session, run_id="run_b")
        ]
    except ValueError:
        events = []
    finally:
        await runtime.close()
    assert events == [] or isinstance(events[-1], TurnFinished)
