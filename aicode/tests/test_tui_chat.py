"""The interactive loop, driven end to end against a scripted model."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from aicode.config import AICodeConfig
from aicode.tui.approve import ConsoleApprovalResolver
from aicode.tui.chat import ChatLoop
from aicode.tui.commands import dispatch
from aicode.tui.console import Console
from aicode.tui.theme import Palette

from aiharness import (
    InMemoryEventStore,
    PermissionMode,
    RunState,
    ToolCallBlock,
)
from aiharness.models.providers.fake import FakeProvider, FakeStep


def loop_for(
    workspace: Path,
    steps: list[FakeStep],
    *,
    answers: list[str] | None = None,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
) -> tuple[ChatLoop, io.StringIO]:
    stream = io.StringIO()
    console = Console(stream, palette=Palette.plain(), animate=False)
    pending = list(answers or [])
    resolver = ConsoleApprovalResolver(
        console,
        workspace=workspace,
        reader=lambda _: pending.pop(0) if pending else "s",
    )
    loop = ChatLoop(
        AICodeConfig(workspace=workspace, unsafe_host=True),
        InMemoryEventStore(),
        console,
        permission_mode=permission_mode,
        resolver=resolver,
        provider=FakeProvider(steps),
    )
    return loop, stream


@pytest.mark.asyncio
async def test_a_turn_streams_text_and_records_usage(tmp_path: Path) -> None:
    loop, stream = loop_for(tmp_path, [FakeStep(text="all done")])

    result = await loop.turn("what is up")

    assert result.state is RunState.COMPLETED
    assert "all done" in stream.getvalue()
    assert loop.turns == 1
    assert loop.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_a_tool_call_shows_its_signature_and_result(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("first\nsecond\n", encoding="utf-8")
    loop, stream = loop_for(
        tmp_path,
        [
            FakeStep(
                tool_calls=(
                    ToolCallBlock(id="t1", name="read_file", input={"path": "hello.txt"}),
                ),
                stop_reason="tool_use",
            ),
            FakeStep(text="read it"),
        ],
    )

    result = await loop.turn("read hello.txt")

    output = stream.getvalue()
    assert result.state is RunState.COMPLETED
    assert "● read_file(hello.txt)" in output
    assert "first" in output


@pytest.mark.asyncio
async def test_an_approval_is_answered_inline(tmp_path: Path) -> None:
    """`bash` always asks; answering here must not suspend the run."""

    loop, stream = loop_for(
        tmp_path,
        [
            FakeStep(
                tool_calls=(ToolCallBlock(id="t1", name="bash", input={"command": "echo hi"}),),
                stop_reason="tool_use",
            ),
            FakeStep(text="ran it"),
        ],
        answers=["y"],
    )

    result = await loop.turn("say hi")

    output = stream.getvalue()
    assert "Approval required bash" in output
    assert "$ echo hi" in output
    assert result.state is RunState.COMPLETED
    assert result.suspended is False


@pytest.mark.asyncio
async def test_refusing_to_answer_suspends_rather_than_failing(tmp_path: Path) -> None:
    loop, stream = loop_for(
        tmp_path,
        [
            FakeStep(
                tool_calls=(ToolCallBlock(id="t1", name="bash", input={"command": "rm -rf /"}),),
                stop_reason="tool_use",
            )
        ],
        answers=["s"],
    )

    result = await loop.turn("do something drastic")

    assert result.suspended is True
    assert result.pending_approval_id is not None
    assert "Waiting for approval" in stream.getvalue()
    # The suspension is durable, not just a message on screen.
    assert any(event.type == "run.suspended" for event in loop.session.events)


@pytest.mark.asyncio
async def test_a_suspended_run_can_be_resumed_from_the_loop(tmp_path: Path) -> None:
    loop, _ = loop_for(
        tmp_path,
        [
            FakeStep(
                tool_calls=(ToolCallBlock(id="t1", name="bash", input={"command": "echo hi"}),),
                stop_reason="tool_use",
            ),
            FakeStep(text="finished"),
        ],
        answers=["s", "y"],
    )
    suspended = await loop.turn("say hi")
    assert suspended.suspended is True

    resumed = await loop.resume(None)

    assert resumed is not None
    assert resumed.state is RunState.COMPLETED
    assert resumed.run_id == suspended.run_id


@pytest.mark.asyncio
async def test_plan_mode_refuses_the_write_without_asking(tmp_path: Path) -> None:
    loop, stream = loop_for(
        tmp_path,
        [
            FakeStep(
                tool_calls=(
                    ToolCallBlock(
                        id="t1", name="write_file", input={"path": "x.txt", "content": "hi"}
                    ),
                ),
                stop_reason="tool_use",
            ),
            FakeStep(text="cannot in plan mode"),
        ],
        permission_mode=PermissionMode.PLAN,
    )

    await loop.turn("write a file")

    assert "denied:" in stream.getvalue()
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_an_interrupted_turn_ends_as_interrupted_not_failed(tmp_path: Path) -> None:
    loop, stream = loop_for(tmp_path, [FakeStep(text="x" * 400, delay_seconds=0.05)])

    result = await asyncio.wait_for(_interrupt_after(loop, "go"), timeout=10)

    assert result.state is RunState.INTERRUPTED
    assert "Interrupted." in stream.getvalue()
    assert any(event.type == "run.interrupted" for event in loop.session.events)


async def _interrupt_after(loop: ChatLoop, text: str) -> object:
    """Trip the same cancel path Esc and Ctrl-C use, mid-stream."""

    original = loop.runtime.coordinator.run
    cancels: list[asyncio.Event] = []

    async def spy(*args: object, **kwargs: object) -> object:
        event = kwargs.get("cancel_event")
        assert isinstance(event, asyncio.Event)
        cancels.append(event)
        asyncio.get_running_loop().call_later(0.1, event.set)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    loop.runtime.coordinator.run = spy  # type: ignore[method-assign]
    try:
        return await loop.turn(text)
    finally:
        loop.runtime.coordinator.run = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_clear_starts_a_new_session_without_touching_the_old_one(tmp_path: Path) -> None:
    loop, _ = loop_for(tmp_path, [FakeStep(text="one")])
    await loop.turn("first")
    first_id = loop.session.id
    first_events = len(loop.session.events)

    await dispatch(loop, "/clear")

    assert loop.session.id != first_id
    assert loop.turns == 0
    assert len(loop.store.read(first_id)) == first_events


@pytest.mark.asyncio
async def test_slash_commands_never_reach_the_model(tmp_path: Path) -> None:
    loop, _ = loop_for(tmp_path, [FakeStep(text="unused")])

    await dispatch(loop, "/mode plan")
    await dispatch(loop, "/model gpt-test")

    assert loop.permission_mode is PermissionMode.PLAN
    assert loop.model == "gpt-test"
    assert loop.turns == 0
    assert loop.session.messages == ()


@pytest.mark.asyncio
async def test_an_unknown_command_is_not_forwarded_as_a_prompt(tmp_path: Path) -> None:
    loop, stream = loop_for(tmp_path, [FakeStep(text="unused")])

    stop = await dispatch(loop, "/nonsense")

    assert stop is False
    assert "Unknown command /nonsense" in stream.getvalue()
    assert loop.turns == 0
