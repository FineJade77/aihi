"""Produce the compatibility corpus by running the harness for real.

The corpus used to be written by hand, so it drifted silently: ADR-0027 changed
the `subagent.*` payloads and every test still passed. Here the events come out
of actual runs, and the frozen file is compared against a fresh build, so a
change on the *writer* side fails until the fixture is regenerated deliberately.

Volatile facts (ids, timestamps, digests, temp paths) are normalized to stable
placeholders; everything a reader could depend on is compared verbatim.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from aihi.agent import (
    AgentBudget,
    ApprovalOutcome,
    ArtifactLifecycle,
    ArtifactPolicy,
    FileArtifactStore,
    HostBackend,
    InMemoryEventStore,
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryScope,
    MemoryService,
    PermissionMode,
    RunCoordinator,
    Session,
    StaticApprovalResolver,
    ToolRegistry,
    WorkspaceScope,
)
from aihi.agent._core.events import Event
from aihi.agent.agents.graph import TaskGraph
from aihi.models import FakeProvider, FakeStep, Message

from packages.aihi.agent.tests.support_tools import ReadTestTool, WriteTestTool

FIXED_TIME = "2026-01-01T00:00:00+00:00"
_GENERATED_ID = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _coordinator(
    tmp_path: Path,
    steps: list[FakeStep],
    *,
    tools: list[Any] | None = None,
    outcome: ApprovalOutcome | None = None,
    **kwargs: Any,
) -> RunCoordinator:
    return RunCoordinator(
        FakeProvider(steps),
        registry=ToolRegistry(
            tools if tools is not None else [WriteTestTool(), ReadTestTool()]
        ),
        sandbox=HostBackend(tmp_path, unsafe=True),
        approval_resolver=None if outcome is None else StaticApprovalResolver(outcome),
        **kwargs,
    )


async def _authorized_session(root: Path, store: InMemoryEventStore) -> Session:
    """Approval, lease, tool lifecycle, artifacts, memory and a subagent record."""

    session = Session.create(
        store, cwd=root, provider="fake", model="fake-model", session_id="ses-golden-a"
    )
    session.add_message(Message(role="system", content=(), metadata={"origin": "project_rules"}))
    artifacts = FileArtifactStore(root / ".artifacts")

    # A lease-gated call: the resolver grants inline, so the runtime issues the
    # capability lease it asked for and then retries the call.
    leased = _coordinator(
        root,
        [
            FakeStep.call_tool("write_file", {"path": "leased.txt", "content": "hello"}),
            FakeStep(text="written"),
        ],
        outcome=ApprovalOutcome.GRANTED,
        artifact_store=artifacts,
    )
    first = await leased.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write with a lease"),
        require_capability_lease=True,
    )
    lease_id = next(iter(session.authorization.leases))
    session.revoke_capability_lease(lease_id, run_id=first.run_id)

    # No resolver: the run suspends, an operator grants once out of band, and
    # the resumed run spends the grant.
    deferred = _coordinator(
        root,
        [
            FakeStep.call_tool("write_file", {"path": "note.txt", "content": "hi"}),
            FakeStep(text="written"),
        ],
        artifact_store=artifacts,
    )
    suspended = await deferred.run(
        session, model="fake-model", user_message=Message.text("user", "write a note")
    )
    assert suspended.pending_approval_id is not None
    session.resolve_approval(
        suspended.pending_approval_id,
        approved=True,
        resolved_by="operator",
        run_id=suspended.run_id,
        one_shot=True,
    )
    await deferred.resume(session, run_id=suspended.run_id, model="fake-model")

    # Artifacts: recorded by the runtime, then deleted through it.
    ref = artifacts.put_text(
        "large tool output " * 100,
        policy=ArtifactPolicy(session_id=session.id, retention="session"),
    )
    session.append(
        Event(
            type="artifact.created",
            session_id=session.id,
            run_id=suspended.run_id,
            data={"artifact": ref.to_dict(), "purpose": "context"},
        )
    )
    ArtifactLifecycle(artifacts, session.id, session.append).delete(
        ref.artifact_id, run_id=suspended.run_id
    )

    # Memory: a candidate, a durable write, then a tombstone.
    access = MemoryAccess(scope_grants=frozenset({(MemoryScope.SESSION, session.id)}))
    memory = MemoryService(
        InMemoryMemoryStore(), event_sink=session.append, write_access=access
    )
    candidates = memory.extract(
        "Remember: the build runs with make check.",
        source="assistant",
        scope=MemoryScope.SESSION,
        scope_id=session.id,
        session_id=session.id,
        run_id=suspended.run_id,
    )
    record = memory.write(candidates[0])
    memory.delete(record.memory_id, access=access, actor="operator", reason="requested")

    # A task graph writing into this session produces the spawn record.
    graph = TaskGraph(session_id=session.id, event_sink=session.append)
    graph.create_root(
        parent_run_id=suspended.run_id,
        objective="investigate",
        budget=AgentBudget(max_tokens=512, timeout_seconds=10.0, max_tool_calls=2),
        workspace=WorkspaceScope(root=str(root), read_only=True),
        capabilities=frozenset({"filesystem.read"}),
    )
    return session


async def _delegating_session(root: Path, store: InMemoryEventStore) -> Session:
    """A child run's own log: subagent start and completion records."""

    from aihi.agent import (
        SPAWN_CAPABILITY,
        ChildRunSubagentRunner,
        SubagentAuthority,
        SubagentTool,
        restrict_registry,
        subagent_session_factory,
    )

    tools = ToolRegistry([ReadTestTool()])
    sandbox = HostBackend(root, unsafe=True)
    runner = ChildRunSubagentRunner(
        lambda spec, child_sandbox: RunCoordinator(
            FakeProvider([FakeStep(text="the child looked around")]),
            registry=restrict_registry(tools, frozenset(spec.capabilities)),
            sandbox=child_sandbox,
        ),
        subagent_session_factory(store, provider="fake", model="fake-model"),
        sandbox=sandbox,
        model="fake-model",
    )
    tool = SubagentTool(
        runner,
        authority=SubagentAuthority(
            budget=AgentBudget(max_tokens=512, timeout_seconds=10.0, max_tool_calls=2),
            workspace=WorkspaceScope(root=str(root), read_only=True),
            capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
        ),
    )
    parent = Session.create(
        store, cwd=root, provider="fake", model="fake-model", session_id="ses-golden-parent"
    )
    coordinator = _coordinator(
        root,
        [FakeStep.call_tool("task", {"objective": "look around"}), FakeStep(text="done")],
        tools=[tool],
        outcome=ApprovalOutcome.GRANTED,
    )
    result = await coordinator.run(
        parent, model="fake-model", user_message=Message.text("user", "delegate")
    )
    child_id = str(parent.messages[-2].tool_results[0].metadata["session_id"])
    assert result.run_id
    return Session.load(store, child_id)


async def _failure_session(root: Path, store: InMemoryEventStore) -> Session:
    """Rejection, compaction, repair, and the three non-happy terminals."""

    session = Session.create(
        store, cwd=root, provider="fake", model="fake-model", session_id="ses-golden-b"
    )
    for index in range(12):
        session.add_message(Message.text("user", f"history {index} " + "x" * 90))

    # An unknown tool is rejected, then the provider fails the run.
    await _coordinator(
        root,
        [FakeStep.call_tool("missing_tool", {}), FakeStep(error=RuntimeError("provider is down"))],
        context_window=700,
        context_safety_margin=0,
    ).run(session, model="fake-model", max_output_tokens=64)

    # Cancellation is an interruption.
    import asyncio

    cancelled = asyncio.Event()
    cancelled.set()
    await _coordinator(root, [FakeStep(text="never")]).run(
        session,
        model="fake-model",
        user_message=Message.text("user", "start then stop"),
        cancel_event=cancelled,
    )

    # A suspended run that the operator abandons.
    abandoning = _coordinator(
        root,
        [
            FakeStep.call_tool("write_file", {"path": "x.txt", "content": "x"}),
            FakeStep(text="ok"),
        ],
    )
    suspended = await abandoning.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "write then give up"),
        permission_mode=PermissionMode.DEFAULT,
    )
    abandoning.abandon(session, run_id=suspended.run_id, reason="operator gave up")
    session.repair_orphan_tool_calls(run_id=suspended.run_id)
    return session


async def build_corpus(root: Path) -> dict[str, Any]:
    """Drive the harness and return the normalized corpus document."""

    store = InMemoryEventStore()
    sessions = [
        await _authorized_session(root, store),
        await _delegating_session(root, store),
        await _failure_session(root, store),
    ]
    forked = sessions[0].fork(at_seq=4, session_id="ses-golden-forked")
    sessions.append(forked)
    document = {
        "schema_version": 1,
        "sessions": [
            {
                "session_id": session.id,
                "events": [event.to_dict() for event in session.events],
            }
            for session in sessions
        ],
    }
    return normalize(document, root)


def normalize(document: Any, root: Path) -> Any:
    """Replace volatile facts with stable placeholders, in first-seen order."""

    seen: dict[str, str] = {}
    counters: dict[str, int] = {}
    # The workspace appears resolved (macOS /private prefix) and unresolved, and
    # inside prose such as "Wrote 5 bytes to <path>.".
    roots = sorted({str(root), str(root.resolve())}, key=len, reverse=True)

    def placeholder(value: str) -> str:
        if value not in seen:
            prefix = value.rsplit("_", 1)[0]
            counters[prefix] = counters.get(prefix, 0) + 1
            seen[value] = f"{prefix}-{counters[prefix]}"
        return seen[value]

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            if _GENERATED_ID.match(value):
                return placeholder(value)
            if _SHA256.match(value):
                return "0" * 64
            for prefix in roots:
                if prefix in value:
                    value = value.replace(prefix, "/workspace")
            if _looks_like_timestamp(value):
                return FIXED_TIME
            return value
        return value

    return walk(document)


def _looks_like_timestamp(value: str) -> bool:
    return len(value) >= 20 and value[4] == "-" and value[7] == "-" and "T" in value


__all__ = ["FIXED_TIME", "build_corpus", "normalize"]


def without_additive_v1_fields(payload: object) -> object:
    """Reduce a fresh corpus to the frozen v1 writer shape.

    The frozen corpus is deliberately legacy: it predates several additive
    fields. Both the compatibility test and `generate_corpus.py` normalize
    through here, so regenerating cannot silently turn an additive field into
    a corpus change.
    """

    normalized = copy.deepcopy(payload)
    if not isinstance(normalized, dict):
        return normalized
    sessions = normalized.get("sessions", [])
    if not isinstance(sessions, list):
        return normalized
    for session in sessions:
        if not isinstance(session, dict):
            continue
        for event in session.get("events", []):
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if isinstance(data, dict):
                if event.get("type") == "model.usage":
                    # Token counts scale with the workspace path baked into the
                    # system prompt, so they differ per machine. The corpus
                    # freezes the payload's shape, not the arithmetic.
                    for volatile in ("input_tokens", "context_tokens"):
                        if volatile in data:
                            data[volatile] = 0
                    for additive in (
                        "cache_write_input_tokens",
                        "cache_enabled",
                        "cache_key_hash",
                        "context_input_capacity",
                        "context_count_method",
                        "context_count_fallback",
                        "context_pressure",
                        "context_projected_pressure",
                        "context_trigger",
                        "context_trigger_reason",
                        "context_target_tokens",
                        "context_target_ratio",
                        "context_pruned_tool_results",
                        "context_reclaimed_tokens",
                        "context_pruning_trigger",
                    ):
                        data.pop(additive, None)
                data.pop("message_schema_version", None)
                data.pop("summary_message_schema_version", None)
                if event.get("type") == "approval.requested":
                    for additive in (
                        "tool_input",
                        "required_capabilities",
                        "sandbox",
                        "execution",
                    ):
                        data.pop(additive, None)
                if event.get("type") == "tool.started":
                    data.pop("execution", None)
                if event.get("type") in {"run.started", "run.resumed"}:
                    data.pop("max_output_tokens", None)
                    data.pop("max_turns", None)
                    data.pop("system_prompt_sha256", None)
                    data.pop("workspace_root", None)
                    data.pop("application_profile", None)
                    descriptor = data.get("sandbox_descriptor")
                    if (
                        isinstance(descriptor, dict)
                        and descriptor.get("mount_scope") == "/workspace"
                    ):
                        descriptor["mount_scope"] = None
    return normalized
