"""Structured rolling-summary contracts and the offline default implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from aihi.models import Message, TextBlock


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """The bounded input supplied to a summary generator.

    A future compact-model adapter can use the same request shape without
    coupling the compiler to a particular provider or network client.
    """

    omitted_messages: tuple[Message, ...]
    retained_messages: tuple[Message, ...]
    system_prompt: str
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredSummary:
    """Schema-stable summary payload embedded in a synthetic system message."""

    #: How this summary was produced. It travels with the payload so a run that
    #: silently fell back from a compact model to the offline generator says so
    #: in its own event log.
    strategy: str = "rolling_summary"
    objective: str = ""
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    verified_state: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    omitted_message_count: int = 0
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "context_compaction_summary",
            "strategy": self.strategy,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "files_changed": list(self.files_changed),
            "verified_state": list(self.verified_state),
            "open_questions": list(self.open_questions),
            "next_steps": list(self.next_steps),
            "skills": list(self.skills),
            "subagents": list(self.subagents),
            "artifacts": list(self.artifacts),
            "omitted_message_count": self.omitted_message_count,
            "fallback_reason": self.fallback_reason,
        }

    def to_message(
        self,
        *,
        source_message_ids: tuple[str, ...],
        strategy: str | None = None,
    ) -> Message:
        strategy = strategy or self.strategy
        text = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return Message(
            role="system",
            content=(TextBlock(text),),
            metadata={
                "compaction": strategy,
                "source_message_ids": list(source_message_ids),
            },
        )


class SummaryGenerator(Protocol):
    """Pluggable summary boundary.

    `generate` is async because a compact model is a network call — the one
    place in context compilation with a real concurrent object to wait on
    (ADR-0029). Offline implementations simply never await.
    """

    async def generate(self, request: SummaryRequest) -> StructuredSummary: ...


class DeterministicSummaryGenerator:
    """Offline, schema-complete fallback used unless a generator is injected."""

    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        objective = next(
            (
                message.text_content
                for message in reversed((*request.omitted_messages, *request.retained_messages))
                if message.role == "user" and message.text_content
            ),
            "",
        )
        return StructuredSummary(
            objective=objective,
            artifacts=request.artifact_ids,
            omitted_message_count=len(request.omitted_messages),
        )


__all__ = [
    "DeterministicSummaryGenerator",
    "StructuredSummary",
    "SummaryGenerator",
    "SummaryRequest",
]
