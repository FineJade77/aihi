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
    ChildRunContext,
    ChildRunSubagentRunner,
    ContextCompiler,
    ContextState,
    InMemoryEventStore,
    RunCoordinator,
    RunState,
    Session,
    StaticApprovalResolver,
    SubagentAuthority,
    SubagentTool,
    ToolRegistry,
    restrict_registry,
)
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

    basic_session = Session.create(InMemoryEventStore())
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

    interrupted_store = InMemoryEventStore()
    interrupted_session = Session.create(interrupted_store)
    cancel = asyncio.Event()
    cancel.set()
    interrupted = await RunCoordinator(
        FakeProvider([FakeStep(text="not reached")]),
        registry=ToolRegistry(),
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
    ).resume(reloaded, run_id=interrupted.run_id, model="fake-model")
    assert resumed.state == RunState.COMPLETED
    assert reloaded.orphan_tool_calls == ()

    compact_session = Session.create(InMemoryEventStore())
    for index in range(20):
        compact_session.add_message(Message.text("user", f"history {index} " + "x" * 80))
    raw_tokens = estimate_messages_tokens(compact_session.messages)
    input_capacity = math.ceil(raw_tokens / 0.88)
    compact = await RunCoordinator(
        FakeProvider([FakeStep(text="compacted")]),
        registry=ToolRegistry(),
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
    full_registry = ToolRegistry()

    def child_session(spec, context):
        return Session.create(
            store,
            metadata={"parent_session_id": context.session_id, "task_id": spec.task_id},
        )

    child_runner = ChildRunSubagentRunner(
        lambda spec: RunCoordinator(
            FakeProvider([FakeStep(text="child done")]),
            registry=restrict_registry(full_registry, frozenset(spec.capabilities)),
        ),
        child_session,
        model="fake-model",
        child_context_factory=lambda spec, context: ChildRunContext(
            app_context=None,
            run_profile={"scope": "read_only_child"},
        ),
    )
    task = SubagentTool(
        child_runner,
        authority=SubagentAuthority(
            budget=AgentBudget(max_tokens=512, timeout_seconds=10, max_tool_calls=2),
            capabilities=frozenset({SPAWN_CAPABILITY}),
        ),
    )
    parent_session = Session.create(store)
    delegated = await RunCoordinator(
        FakeProvider(
            [FakeStep.call_tool("task", {"objective": "inspect"}), FakeStep(text="parent done")]
        ),
        registry=ToolRegistry([task]),
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
