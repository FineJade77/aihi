"""Context budgeting, artifactization, and L1/L2 compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

from aiharness.artifacts import ArtifactPolicy, ArtifactRef, ArtifactStore
from aiharness.context.summary import (
    DeterministicSummaryGenerator,
    SummaryGenerator,
    SummaryRequest,
)
from aiharness.core.errors import ContextWindowExceeded
from aiharness.core.tokens import estimate_messages_tokens, estimate_text_tokens
from aiharness.core.types import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    reserved_output: int
    tool_schema_tokens: int = 0
    safety_margin: int = 256

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.reserved_output < 0 or self.tool_schema_tokens < 0 or self.safety_margin < 0:
            raise ValueError("Context budget components cannot be negative")
        if self.usable_input <= 0:
            raise ValueError("Context budget leaves no usable input capacity")

    @property
    def usable_input(self) -> int:
        return (
            self.context_window
            - self.reserved_output
            - self.tool_schema_tokens
            - self.safety_margin
        )

    @classmethod
    def for_request(
        cls,
        *,
        context_window: int,
        reserved_output: int,
        tools: tuple[ToolSpec, ...] = (),
        safety_margin: int = 256,
    ) -> ContextBudget:
        tool_schema_tokens = estimate_text_tokens(
            json.dumps([tool.to_dict() for tool in tools], sort_keys=True, separators=(",", ":"))
        )
        return cls(
            context_window=context_window,
            reserved_output=reserved_output,
            tool_schema_tokens=tool_schema_tokens,
            safety_margin=safety_margin,
        )


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    strategy: str
    version: int
    replaced_message_ids: tuple[str, ...]
    summary: Message
    before_tokens: int
    after_tokens: int
    artifact_ids: tuple[str, ...] = ()
    prompt_hash: str = ""
    trigger: str = "budget"

    def to_event_data(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "replaced_message_ids": list(self.replaced_message_ids),
            "summary": self.summary.to_dict(),
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "artifact_ids": list(self.artifact_ids),
            "prompt_hash": self.prompt_hash,
            "trigger": self.trigger,
        }


@dataclass(frozen=True, slots=True)
class ContextSection:
    """A named block prepended to the system prompt by a runtime extension.

    The compiler stays domain-agnostic: skills, memory and future contributors
    all arrive as already-rendered text, so `context` never imports them.
    """

    title: str
    body: str
    source: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Context section title must not be empty")

    def render(self) -> str:
        return f"## {self.title.strip()}\n{self.body.strip()}"


def compose_system_prompt(
    system_prompt: str, sections: tuple[ContextSection, ...] | list[ContextSection]
) -> str:
    """Join the base prompt with section blocks in the order they were given."""

    blocks = [system_prompt.strip()] if system_prompt.strip() else []
    blocks.extend(section.render() for section in sections if section.body.strip())
    return "\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class CompiledContext:
    system_prompt: str
    messages: tuple[Message, ...]
    estimated_tokens: int
    budget: ContextBudget
    artifacts: tuple[ArtifactRef, ...] = ()
    compaction: CompactionRecord | None = None

    @property
    def over_budget(self) -> bool:
        return self.estimated_tokens > self.budget.usable_input


class ContextCompiler:
    """Compile messages without network calls and preserve tool-call boundaries."""

    def __init__(
        self,
        *,
        artifact_threshold_tokens: int = 1_024,
        artifact_preview_chars: int = 4_000,
        summary_generator: SummaryGenerator | None = None,
    ) -> None:
        if artifact_threshold_tokens <= 0 or artifact_preview_chars <= 0:
            raise ValueError("Artifact thresholds must be positive")
        self.artifact_threshold_tokens = artifact_threshold_tokens
        self.artifact_preview_chars = artifact_preview_chars
        self.summary_generator = summary_generator or DeterministicSummaryGenerator()

    def compile(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        tools: tuple[ToolSpec, ...],
        budget: ContextBudget,
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        sections: tuple[ContextSection, ...] = (),
    ) -> CompiledContext:
        original = tuple(messages)
        system_prompt = compose_system_prompt(system_prompt, sections)
        materialized, artifacts = self._artifactize(original, artifact_store, artifact_policy)
        before_tokens = self._total_tokens(system_prompt, materialized, budget)
        if before_tokens <= budget.usable_input:
            return CompiledContext(
                system_prompt=system_prompt,
                messages=materialized,
                estimated_tokens=before_tokens,
                budget=budget,
                artifacts=artifacts,
            )

        compacted, replaced_ids = self._compact(materialized, system_prompt, budget)
        after_tokens = self._total_tokens(system_prompt, compacted, budget)
        if not replaced_ids:
            if after_tokens > budget.usable_input:
                raise ContextWindowExceeded(
                    "Context cannot be reduced below the configured input budget",
                    details={
                        "estimated_tokens": after_tokens,
                        "usable_input": budget.usable_input,
                    },
                )
            return CompiledContext(
                system_prompt=system_prompt,
                messages=compacted,
                estimated_tokens=after_tokens,
                budget=budget,
                artifacts=artifacts,
            )
        summary = compacted[0]
        record = CompactionRecord(
            strategy="l1_deterministic",
            version=1,
            replaced_message_ids=tuple(replaced_ids),
            summary=summary,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            artifact_ids=tuple(ref.artifact_id for ref in artifacts),
            prompt_hash=_prompt_hash(system_prompt, materialized, tools, budget),
        )
        if after_tokens > budget.usable_input:
            raise ContextWindowExceeded(
                "Context cannot be reduced below the configured input budget",
                details={
                    "estimated_tokens": after_tokens,
                    "usable_input": budget.usable_input,
                },
            )
        return CompiledContext(
            system_prompt=system_prompt,
            messages=compacted,
            estimated_tokens=after_tokens,
            budget=budget,
            artifacts=artifacts,
            compaction=record,
        )

    def compact_l2(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        tools: tuple[ToolSpec, ...],
        budget: ContextBudget,
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        sections: tuple[ContextSection, ...] = (),
        summary_generator: SummaryGenerator | None = None,
        trigger: str = "provider_context_length",
    ) -> CompiledContext:
        """Force one structured-summary compaction for a context retry.

        L2 deliberately omits at least one message group even when L1 would
        fit. This makes a provider-reported context-length failure actionable
        while retaining tool call/result groups atomically.
        """

        original = tuple(messages)
        system_prompt = compose_system_prompt(system_prompt, sections)
        materialized, artifacts = self._artifactize(original, artifact_store, artifact_policy)
        before_tokens = self._total_tokens(system_prompt, materialized, budget)
        groups = _message_groups(materialized)
        if len(groups) < 2:
            raise ContextWindowExceeded(
                "Context cannot be reduced because it has fewer than two message groups",
                details={"estimated_tokens": before_tokens, "usable_input": budget.usable_input},
            )

        message_limit = max(1, budget.usable_input - estimate_text_tokens(system_prompt))
        generator = summary_generator or self.summary_generator
        omitted = tuple(message for group in groups[:-1] for message in group)
        retained = groups[-1]
        summary = generator.generate(
            SummaryRequest(
                omitted_messages=omitted,
                retained_messages=retained,
                system_prompt=system_prompt,
                artifact_ids=tuple(ref.artifact_id for ref in artifacts),
            )
        )
        summary_message = summary.to_message(
            source_message_ids=tuple(message.id for message in omitted),
        )
        candidate = (summary_message, *retained)
        after_tokens = estimate_messages_tokens(candidate)
        if after_tokens > message_limit:
            raise ContextWindowExceeded(
                "Structured context compaction cannot fit the configured input budget",
                details={"estimated_tokens": before_tokens, "usable_input": budget.usable_input},
            )
        record = CompactionRecord(
            strategy="l2_structured",
            version=1,
            replaced_message_ids=tuple(message.id for message in omitted),
            summary=summary_message,
            before_tokens=before_tokens,
            after_tokens=self._total_tokens(system_prompt, candidate, budget),
            artifact_ids=tuple(ref.artifact_id for ref in artifacts),
            prompt_hash=_prompt_hash(system_prompt, materialized, tools, budget),
            trigger=trigger,
        )
        return CompiledContext(
            system_prompt=system_prompt,
            messages=candidate,
            estimated_tokens=record.after_tokens,
            budget=budget,
            artifacts=artifacts,
            compaction=record,
        )

    def _artifactize(
        self,
        messages: tuple[Message, ...],
        artifact_store: ArtifactStore | None,
        artifact_policy: ArtifactPolicy | None,
    ) -> tuple[tuple[Message, ...], tuple[ArtifactRef, ...]]:
        if artifact_store is None:
            return messages, ()
        artifacts: list[ArtifactRef] = []
        materialized: list[Message] = []
        for message in messages:
            blocks: list[ContentBlock] = []
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    blocks.append(block)
                    continue
                if estimate_text_tokens(block.content) <= self.artifact_threshold_tokens:
                    blocks.append(block)
                    continue
                metadata = {"tool_call_id": block.tool_call_id, "is_error": block.is_error}
                if artifact_policy is None:
                    ref = artifact_store.put_text(block.content, metadata=metadata)
                else:
                    ref = artifact_store.put_text(
                        block.content,
                        metadata=metadata,
                        policy=artifact_policy,
                    )
                artifacts.append(ref)
                preview = block.content[: self.artifact_preview_chars]
                if len(preview) < len(block.content):
                    preview += "\n\n[Full tool output stored as an artifact.]"
                blocks.append(
                    replace(
                        block,
                        content=preview,
                        metadata={
                            **block.metadata,
                            "artifact_id": ref.artifact_id,
                            "artifact_sha256": ref.sha256,
                            "artifact_size_bytes": ref.size_bytes,
                        },
                    )
                )
            materialized.append(replace(message, content=tuple(blocks)))
        return tuple(materialized), tuple(artifacts)

    def _compact(
        self,
        messages: tuple[Message, ...],
        system_prompt: str,
        budget: ContextBudget,
    ) -> tuple[tuple[Message, ...], list[str]]:
        groups = _message_groups(messages)
        if not groups:
            return messages, []
        message_limit = max(1, budget.usable_input - estimate_text_tokens(system_prompt))
        for keep_count in range(len(groups) - 1, 0, -1):
            selected = groups[-keep_count:]
            omitted = [
                message for group in groups[: len(groups) - keep_count] for message in group
            ]
            summary = _summary_message(omitted, messages)
            candidate = (summary, *(message for group in selected for message in group))
            if estimate_messages_tokens(candidate) <= message_limit:
                return candidate, [message.id for message in omitted]
        omitted = [message for group in groups[:-1] for message in group]
        summary = _summary_message(omitted, messages)
        return (summary, *groups[-1]), [message.id for message in omitted]

    @staticmethod
    def _total_tokens(
        system_prompt: str,
        messages: tuple[Message, ...],
        budget: ContextBudget,
    ) -> int:
        return estimate_text_tokens(system_prompt) + estimate_messages_tokens(messages)


def _message_groups(messages: tuple[Message, ...]) -> list[tuple[Message, ...]]:
    """Group each assistant tool call with all following results atomically."""

    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages):
        start = index
        pending = {call.id for call in messages[index].tool_calls}
        pending.difference_update(result.tool_call_id for result in messages[index].tool_results)
        index += 1
        while pending and index < len(messages):
            pending.difference_update(
                result.tool_call_id for result in messages[index].tool_results
            )
            pending.update(call.id for call in messages[index].tool_calls)
            index += 1
        groups.append(messages[start:index])
    return groups


def _summary_message(omitted: list[Message], all_messages: tuple[Message, ...]) -> Message:
    objective = next(
        (message.text_content for message in reversed(all_messages) if message.role == "user"),
        "",
    )
    payload: dict[str, Any] = {
        "kind": "context_compaction_summary",
        "objective": objective,
        "constraints": [],
        "decisions": [],
        "files_changed": [],
        "verified_state": [],
        "open_questions": [],
        "next_steps": [],
        "permission_mode": None,
        "skills": [],
        "subagents": [],
        "artifacts": [],
        "omitted_message_count": len(omitted),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return Message(
        role="system",
        content=(TextBlock(text),),
        metadata={
            "compaction": "l1_deterministic",
            "source_message_ids": [message.id for message in omitted],
        },
    )


def _prompt_hash(
    system_prompt: str,
    messages: tuple[Message, ...],
    tools: tuple[ToolSpec, ...],
    budget: ContextBudget,
) -> str:
    material = json.dumps(
        {
            "system_prompt": system_prompt,
            "messages": [message.to_dict() for message in messages],
            "tools": [tool.to_dict() for tool in tools],
            "usable_input": budget.usable_input,
            "reserved_output": budget.reserved_output,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()
