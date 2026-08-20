"""Installed-wheel integration smoke; executed in an isolated virtual environment."""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

from aihi.agent import (
    SPAWN_CAPABILITY,
    AgentBudget,
    ApprovalOutcome,
    ChildRunSubagentRunner,
    ContextCompiler,
    ContextState,
    HostBackend,
    InMemoryEventStore,
    ReadFileTool,
    RunCoordinator,
    RunState,
    Session,
    StaticApprovalResolver,
    SubagentAuthority,
    SubagentTool,
    ToolRegistry,
    WorkspaceScope,
    WriteFileTool,
    restrict_registry,
    subagent_session_factory,
)
from aihi.code_agent.config import CodeAgentConfig
from aihi.models import (
    Capabilities,
    FakeProvider,
    FakeStep,
    Message,
    Usage,
    estimate_messages_tokens,
)


async def main(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    assert CodeAgentConfig.defaults(workspace).audit_path == workspace / ".aihi" / "audit.jsonl"
    sandbox = HostBackend(workspace, unsafe=True)

    basic_session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )
    cache_provider = FakeProvider(
        [
            FakeStep(
                text="done",
                usage=Usage(
                    input_tokens=100,
                    output_tokens=5,
                    cached_input_tokens=60,
                    cache_write_input_tokens=20,
                ),
            )
        ],
        capabilities=Capabilities(prefix_caching=True, token_counting=True),
    )
    basic = await RunCoordinator(
        cache_provider,
        registry=ToolRegistry(),
        sandbox=sandbox,
    ).run(
        basic_session,
        model="fake-model",
        user_message=Message.text("user", "hello"),
        system_prompt="stable wheel smoke instructions",
    )
    assert basic.state == RunState.COMPLETED
    cache_usage = next(
        event for event in basic_session.events if event.type == "model.usage"
    )
    assert cache_usage.data["cached_input_tokens"] == 60
    assert cache_usage.data["cache_write_input_tokens"] == 20
    assert isinstance(cache_usage.data["cache_key_hash"], str)

    (workspace / "note.txt").write_text("wheel smoke", encoding="utf-8")
    tool_session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )
    tool_run = await RunCoordinator(
        FakeProvider(
            [
                FakeStep.call_tool("read_file", {"path": "note.txt"}),
                FakeStep(text="read"),
            ]
        ),
        registry=ToolRegistry([ReadFileTool()]),
        sandbox=sandbox,
    ).run(
        tool_session,
        model="fake-model",
        user_message=Message.text("user", "read note.txt"),
    )
    assert tool_run.state == RunState.COMPLETED
    assert any(
        "wheel smoke" in result.content
        for message in tool_session.messages
        for result in message.tool_results
    )

    approval_session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )
    approval = await RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("write_file", {"path": "blocked.txt", "content": "x"})]
        ),
        registry=ToolRegistry([WriteFileTool()]),
        sandbox=sandbox,
    ).run(
        approval_session,
        model="fake-model",
        user_message=Message.text("user", "write"),
    )
    assert approval.state == RunState.WAITING_APPROVAL
    assert not (workspace / "blocked.txt").exists()

    interrupted_store = InMemoryEventStore()
    interrupted_session = Session.create(
        interrupted_store, cwd=workspace, provider="fake", model="fake-model"
    )
    cancel = asyncio.Event()
    cancel.set()
    interrupted = await RunCoordinator(
        FakeProvider([FakeStep(text="not reached")]),
        registry=ToolRegistry(),
        sandbox=sandbox,
    ).run(
        interrupted_session,
        model="fake-model",
        user_message=Message.text("user", "pause"),
        cancel_event=cancel,
    )
    assert interrupted.state == RunState.INTERRUPTED
    reloaded = Session.load(interrupted_store, interrupted_session.id)
    resumed = await RunCoordinator(
        FakeProvider([FakeStep(text="resumed")]),
        registry=ToolRegistry(),
        sandbox=sandbox,
    ).resume(reloaded, run_id=interrupted.run_id, model="fake-model")
    assert resumed.state == RunState.COMPLETED
    assert reloaded.orphan_tool_calls == ()

    compact_session = Session.create(
        InMemoryEventStore(), cwd=workspace, provider="fake", model="fake-model"
    )
    for index in range(20):
        compact_session.add_message(Message.text("user", f"history {index} " + "x" * 80))
    raw_tokens = estimate_messages_tokens(compact_session.messages)
    input_capacity = math.ceil(raw_tokens / 0.88)
    compact = await RunCoordinator(
        FakeProvider([FakeStep(text="compacted")]),
        registry=ToolRegistry(),
        sandbox=sandbox,
        context_compiler=ContextCompiler(),
        context_window=input_capacity + 64,
        context_safety_margin=0,
    ).run(compact_session, model="fake-model", max_output_tokens=64)
    assert compact.state == RunState.COMPLETED
    compaction = next(
        event for event in compact_session.events if event.type == "compaction.created"
    )
    assert compaction.data["version"] == 2
    assert ContextState.from_dict(compaction.data["context_state"]).schema_version == 2

    store = InMemoryEventStore()
    full_registry = ToolRegistry([ReadFileTool()])
    child_runner = ChildRunSubagentRunner(
        lambda spec, child_sandbox: RunCoordinator(
            FakeProvider([FakeStep(text="child done")]),
            registry=restrict_registry(full_registry, frozenset(spec.capabilities)),
            sandbox=child_sandbox,
        ),
        subagent_session_factory(store, provider="fake", model="fake-model"),
        sandbox=sandbox,
        model="fake-model",
    )
    task = SubagentTool(
        child_runner,
        authority=SubagentAuthority(
            budget=AgentBudget(max_tokens=512, timeout_seconds=10, max_tool_calls=2),
            workspace=WorkspaceScope(root=str(workspace), read_only=True),
            capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
        ),
    )
    parent_session = Session.create(
        store, cwd=workspace, provider="fake", model="fake-model"
    )
    delegated = await RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("task", {"objective": "inspect"}), FakeStep(text="parent done")]
        ),
        registry=ToolRegistry([task]),
        sandbox=sandbox,
        approval_resolver=StaticApprovalResolver(ApprovalOutcome.GRANTED),
    ).run(
        parent_session,
        model="fake-model",
        user_message=Message.text("user", "delegate"),
    )
    assert delegated.state == RunState.COMPLETED
    assert any(message.tool_results for message in parent_session.messages)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
