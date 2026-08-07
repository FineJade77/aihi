"""Read-only tool calls run together; anything that mutates runs alone."""

import asyncio
from pathlib import Path

import pytest

from aiharness import (
    FakeProvider,
    HostBackend,
    InMemoryEventStore,
    Message,
    RunCoordinator,
    RunState,
    Session,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from aiharness.core.ids import new_id
from aiharness.core.types import ToolCallBlock
from aiharness.models.providers.fake import FakeStep


class TracingTool:
    """Record entry/exit order so overlap is observable."""

    def __init__(self, name: str, *, concurrency_safe: bool, mutates: bool) -> None:
        self.spec = ToolSpec(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {"delay": {"type": "number"}}},
            concurrency_safe=concurrency_safe,
            mutates=mutates,
        )
        self.trace: list[str] = []

    async def run(self, input: dict[str, object], context: ToolContext) -> ToolResult:
        self.trace.append(f"enter:{self.spec.name}")
        await asyncio.sleep(float(input.get("delay", 0.02)))
        self.trace.append(f"exit:{self.spec.name}")
        return ToolResult(content=self.spec.name)


def session_for(tmp_path: Path, name: str) -> Session:
    return Session.create(
        InMemoryEventStore(), cwd=tmp_path, provider="fake", model="fake-model", session_id=name
    )


def call(name: str) -> ToolCallBlock:
    return ToolCallBlock(new_id("toolu"), name, {})


async def run_with(tmp_path: Path, tools: list[TracingTool], names: list[str], session_id: str):
    shared: list[str] = []
    for tool in tools:
        tool.trace = shared
    registry = ToolRegistry(tools)  # type: ignore[arg-type]
    provider = FakeProvider(
        [
            FakeStep(tool_calls=tuple(call(name) for name in names), stop_reason="tool_use"),
            FakeStep(text="done"),
        ]
    )
    coordinator = RunCoordinator(
        provider,
        registry=registry,
        sandbox=HostBackend(tmp_path, unsafe=True),
    )
    session = session_for(tmp_path, session_id)
    result = await coordinator.run(
        session, model="fake-model", user_message=Message.text("user", "go")
    )
    return result, session, shared


@pytest.mark.asyncio
async def test_read_only_calls_overlap(tmp_path: Path) -> None:
    tools = [
        TracingTool("read_a", concurrency_safe=True, mutates=False),
        TracingTool("read_b", concurrency_safe=True, mutates=False),
        TracingTool("read_c", concurrency_safe=True, mutates=False),
    ]

    result, session, trace = await run_with(
        tmp_path, tools, ["read_a", "read_b", "read_c"], "ses-parallel"
    )

    assert result.state == RunState.COMPLETED
    # All three entered before any of them finished.
    assert trace[:3] == ["enter:read_a", "enter:read_b", "enter:read_c"]
    # Results are still committed in call order.
    contents = [
        message.tool_results[0].content for message in session.messages if message.tool_results
    ]
    assert contents == ["read_a", "read_b", "read_c"]


@pytest.mark.asyncio
async def test_a_mutating_call_runs_alone(tmp_path: Path) -> None:
    tools = [
        TracingTool("read_a", concurrency_safe=True, mutates=False),
        TracingTool("write_b", concurrency_safe=False, mutates=True),
        TracingTool("read_c", concurrency_safe=True, mutates=False),
    ]

    result, _, trace = await run_with(
        tmp_path, tools, ["read_a", "write_b", "read_c"], "ses-serial"
    )

    assert result.state == RunState.WAITING_APPROVAL  # the mutating call needs approval
    # The read ran and finished before the mutating call was even considered.
    assert trace == ["enter:read_a", "exit:read_a"]


@pytest.mark.asyncio
async def test_reads_before_a_mutating_call_are_grouped(tmp_path: Path) -> None:
    tools = [
        TracingTool("read_a", concurrency_safe=True, mutates=False),
        TracingTool("read_b", concurrency_safe=True, mutates=False),
        TracingTool("write_c", concurrency_safe=False, mutates=True),
    ]

    result, session, trace = await run_with(
        tmp_path, tools, ["read_a", "read_b", "write_c"], "ses-mixed"
    )

    assert trace[:2] == ["enter:read_a", "enter:read_b"]
    # The run suspends on the mutating call, but both reads are already committed.
    assert result.state == RunState.WAITING_APPROVAL
    contents = [
        message.tool_results[0].content for message in session.messages if message.tool_results
    ]
    assert contents == ["read_a", "read_b"]
    assert len(result.pending_tool_call_ids) == 1


@pytest.mark.asyncio
async def test_unknown_tools_do_not_join_a_group(tmp_path: Path) -> None:
    tools = [TracingTool("read_a", concurrency_safe=True, mutates=False)]

    result, session, _ = await run_with(tmp_path, tools, ["read_a", "missing"], "ses-unknown")

    assert result.state == RunState.COMPLETED
    results = [message.tool_results[0] for message in session.messages if message.tool_results]
    assert [item.is_error for item in results] == [False, True]
    assert results[1].metadata["error_code"] == "tool_not_found"
