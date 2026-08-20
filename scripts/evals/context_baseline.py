"""Deterministic long-session cache/compaction evaluator for H-19."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from aihi.agent import (
    EventStore,
    HostBackend,
    RunCoordinator,
    RunState,
    Session,
    ToolRegistry,
)
from aihi.agent.context import StructuredSummary, SummaryRequest
from aihi.code_agent.evals import CodeTask, TaskExecution
from aihi.code_agent.prompts import load_builtin_prompt
from aihi.models import (
    Capabilities,
    FakeProvider,
    FakeStep,
    Message,
    MessageEnd,
    ModelRequest,
    StreamChunk,
    Usage,
    estimate_messages_tokens,
    estimate_model_request_tokens,
    estimate_text_tokens,
)

_CRITICAL_CONSTRAINT = "Preserve checkpoint H19-CONTEXT-ALPHA."
_MODEL = "context-eval-model"


class _ContextEvalSummaryGenerator:
    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        omitted_text = "\n".join(message.text_content for message in request.omitted_messages)
        constraints = (
            (_CRITICAL_CONSTRAINT,) if _CRITICAL_CONSTRAINT in omitted_text else ()
        )
        objective = next(
            (
                message.text_content
                for message in reversed(request.retained_messages)
                if message.role == "user" and message.text_content
            ),
            "",
        )
        return StructuredSummary(
            strategy="l2_context_eval",
            objective=objective,
            constraints=constraints,
            omitted_message_count=len(request.omitted_messages),
        )


class _CacheHitFakeProvider(FakeProvider):
    """Report usage from the normalized request while simulating a cache hit."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamChunk]:
        async for chunk in super().stream(request):
            if not isinstance(chunk, MessageEnd):
                yield chunk
                continue
            input_tokens = estimate_model_request_tokens(request)
            usage = Usage(
                input_tokens=input_tokens,
                output_tokens=chunk.response.usage.output_tokens,
                cached_input_tokens=max(1, input_tokens // 2),
                cache_write_input_tokens=0,
            )
            yield MessageEnd(replace(chunk.response, usage=usage))


def _history() -> tuple[Message, ...]:
    messages: list[Message] = []
    for index in range(28):
        marker = f"{_CRITICAL_CONSTRAINT} " if index == 0 else ""
        messages.append(
            Message.text(
                "user",
                f"{marker}Long-session checkpoint {index:02d}: " + "context " * 45,
            )
        )
        messages.append(
            Message.text(
                "assistant",
                f"Recorded checkpoint {index:02d}. " + "evidence " * 35,
            )
        )
    return tuple(messages)


async def context_reference_executor(
    task: CodeTask, workspace: Path, store: EventStore
) -> TaskExecution:
    """Execute the same task above and below the hard-compaction threshold."""

    session = Session.create(store, cwd=workspace, provider="fake", model=_MODEL)
    for message in _history():
        session.add_message(message)

    system_prompt = load_builtin_prompt()
    incoming = Message.text("user", task.prompt)
    raw_input_tokens = estimate_text_tokens(system_prompt) + estimate_messages_tokens(
        (*session.messages, incoming)
    )
    compacted_profile = task.case_id == "long-session-compacted"
    input_capacity = (
        math.ceil(raw_input_tokens / 0.88) if compacted_profile else 127_744
    )
    context_window = input_capacity + task.max_tokens
    provider = _CacheHitFakeProvider(
        [FakeStep(text="context preserved")],
        capabilities=Capabilities(
            prefix_caching=True,
            token_counting=True,
            max_context=context_window,
            max_output=task.max_tokens,
        ),
    )
    coordinator = RunCoordinator(
        provider,
        registry=ToolRegistry(),
        sandbox=HostBackend(workspace, unsafe=True),
        context_window=context_window,
        context_safety_margin=0,
        summary_generator=_ContextEvalSummaryGenerator(),
    )
    result = await coordinator.run(
        session,
        model=_MODEL,
        user_message=incoming,
        system_prompt=system_prompt,
        max_turns=task.max_turns,
        max_output_tokens=task.max_tokens,
    )
    if result.state is RunState.COMPLETED:
        (workspace / "answer.txt").write_text("ok\n", encoding="utf-8")

    compactions = tuple(
        event for event in session.events if event.type == "compaction.created"
    )
    if compacted_profile:
        if not compactions or compactions[-1].data.get("version") != 2:
            raise ValueError("compacted context profile did not create ContextState v2")
    elif compactions:
        raise ValueError("uncompacted context profile unexpectedly compacted")

    rendered = "\n".join(
        message.text_content
        for request in provider.requests
        for message in request.messages
    )
    markers = (_CRITICAL_CONSTRAINT, task.prompt)
    critical_state_recall = sum(marker in rendered for marker in markers) / len(markers)
    return TaskExecution(
        session=session,
        run_result=result,
        metrics={"critical_state_recall": critical_state_recall},
    )


__all__ = ["context_reference_executor"]
