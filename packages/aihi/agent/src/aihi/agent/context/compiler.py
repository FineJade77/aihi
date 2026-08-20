"""Context budgeting, artifactization, and L1/L2 compaction."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any

from aihi.agent._core.errors import ContextWindowExceeded
from aihi.agent._core.events import Event
from aihi.agent.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRef, ArtifactStore
from aihi.agent.context.policy import CompactionPolicy, ContextPressure
from aihi.agent.context.projector import legacy_summary_state, project_context_state
from aihi.agent.context.state import ContextState
from aihi.agent.context.summary import (
    DeterministicSummaryGenerator,
    SummaryGenerator,
    SummaryRequest,
)
from aihi.agent.tools.spec import ToolSpec
from aihi.models import (
    MESSAGE_SCHEMA_VERSION,
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    estimate_messages_tokens,
    estimate_text_tokens,
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
        """Legacy message capacity after reserving the tool schema."""

        return self.input_capacity - self.tool_schema_tokens

    @property
    def input_capacity(self) -> int:
        """Prompt capacity after output and safety reservations."""

        return (
            self.context_window
            - self.reserved_output
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
    retained_message_ids: tuple[str, ...] = ()
    context_state: ContextState | None = None
    source_message_ids: tuple[str, ...] = ()
    source_event_seqs: tuple[int, ...] = ()
    policy_snapshot: dict[str, object] | None = None
    token_count_method: str = "estimate"
    stable_prefix_hash: str = ""
    cache_epoch_hash: str = ""
    summary_generator: str = ""
    fallback_reason: str | None = None

    def to_event_data(self) -> dict[str, object]:
        value: dict[str, object] = {
            "strategy": self.strategy,
            "version": self.version,
            "replaced_message_ids": list(self.replaced_message_ids),
            "summary": self.summary.to_dict(),
            "summary_message_schema_version": MESSAGE_SCHEMA_VERSION,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "artifact_ids": list(self.artifact_ids),
            "prompt_hash": self.prompt_hash,
            "trigger": self.trigger,
        }
        if self.version >= 2:
            value.update(
                {
                    "strategy_version": self.version,
                    "retained_message_ids": list(self.retained_message_ids),
                    "context_state": (
                        self.context_state.to_dict() if self.context_state is not None else None
                    ),
                    "source_message_ids": list(self.source_message_ids),
                    "source_event_seqs": list(self.source_event_seqs),
                    "policy_snapshot": dict(self.policy_snapshot or {}),
                    "token_count_method": self.token_count_method,
                    "stable_prefix_hash": self.stable_prefix_hash,
                    "cache_epoch_hash": self.cache_epoch_hash,
                    "summary_generator": self.summary_generator,
                    "fallback_reason": self.fallback_reason,
                }
            )
        return value


@dataclass(frozen=True, slots=True)
class ToolResultPruningRecord:
    """One deterministic batch of recoverable Tool Result body removals."""

    tool_call_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int
    reclaimed_tokens: int
    trigger: str = "soft"
    version: int = 1


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

    return "\n\n".join(
        block.text for block in compose_system_blocks(system_prompt, sections)
    )


def compose_system_blocks(
    system_prompt: str, sections: tuple[ContextSection, ...] | list[ContextSection]
) -> tuple[TextBlock, ...]:
    """Keep the base prompt stable and extension sections in the dynamic suffix."""

    blocks: list[TextBlock] = []
    if system_prompt.strip():
        blocks.append(TextBlock(system_prompt.strip(), stable_prefix=True))
    blocks.extend(
        TextBlock(section.render()) for section in sections if section.body.strip()
    )
    return tuple(blocks)


@dataclass(frozen=True, slots=True)
class CompiledContext:
    system_prompt: str
    messages: tuple[Message, ...]
    estimated_tokens: int
    budget: ContextBudget
    system_blocks: tuple[TextBlock, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    compaction: CompactionRecord | None = None
    pressure: ContextPressure | None = None
    pruning: ToolResultPruningRecord | None = None

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
        system_blocks = compose_system_blocks(system_prompt, sections)
        system_prompt = "\n\n".join(block.text for block in system_blocks)
        materialized, artifacts = self._artifactize(original, artifact_store, artifact_policy)
        before_tokens = self._total_tokens(system_prompt, materialized, budget)
        if before_tokens <= budget.usable_input:
            return CompiledContext(
                system_prompt=system_prompt,
                messages=materialized,
                estimated_tokens=before_tokens,
                budget=budget,
                system_blocks=system_blocks,
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
                system_blocks=system_blocks,
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
            system_blocks=system_blocks,
            artifacts=artifacts,
            compaction=record,
        )

    def prune_tool_results(
        self,
        compiled: CompiledContext,
        *,
        artifact_store: ArtifactStore,
        artifact_access: ArtifactAccess,
        tools: tuple[ToolSpec, ...],
        policy: CompactionPolicy,
        durable_message_ids: frozenset[str],
        trigger: str = "soft",
    ) -> CompiledContext:
        """Batch-remove old recoverable read-only Tool Result bodies.

        This edits only the model-input projection. The durable Message Events
        and Artifact payloads remain untouched and are the recovery source.
        """

        groups = _message_groups(compiled.messages)
        protected = _recent_group_indexes(groups, compiled.budget.input_capacity, policy)
        specs = {tool.name: tool for tool in tools}
        message_indexes = {message.id: index for index, message in enumerate(compiled.messages)}
        replacements: dict[int, dict[int, ToolResultBlock]] = {}
        tool_call_ids: list[str] = []
        artifact_ids: list[str] = []

        for group_index, group in enumerate(groups):
            if group_index in protected:
                continue
            calls = {call.id: call for message in group for call in message.tool_calls}
            call_message_ids = {
                call.id: message.id for message in group for call in message.tool_calls
            }
            result_counts: dict[str, int] = {}
            for message in group:
                for result in message.tool_results:
                    result_counts[result.tool_call_id] = (
                        result_counts.get(result.tool_call_id, 0) + 1
                    )
            for message in group:
                message_index = message_indexes[message.id]
                for block_index, block in enumerate(message.content):
                    if not isinstance(block, ToolResultBlock):
                        continue
                    call = calls.get(block.tool_call_id)
                    spec = specs.get(call.name) if call is not None else None
                    if (
                        call is None
                        or spec is None
                        or spec.mutates
                        or block.is_error
                        or result_counts.get(block.tool_call_id) != 1
                        or message.id not in durable_message_ids
                        or call_message_ids.get(block.tool_call_id) not in durable_message_ids
                        or block.metadata.get("context_pruned") is True
                    ):
                        continue
                    artifact = self._recoverable_artifact(
                        block,
                        artifact_store=artifact_store,
                        artifact_access=artifact_access,
                    )
                    if artifact is None:
                        continue
                    placeholder = _tool_result_placeholder(call.name, artifact)
                    if estimate_text_tokens(placeholder) >= estimate_text_tokens(block.content):
                        continue
                    replacements.setdefault(message_index, {})[block_index] = replace(
                        block,
                        content=placeholder,
                        metadata={
                            **block.metadata,
                            "context_pruned": True,
                            "context_pruning_version": 1,
                        },
                    )
                    tool_call_ids.append(block.tool_call_id)
                    artifact_ids.append(artifact.artifact_id)

        if not replacements:
            return compiled
        messages = list(compiled.messages)
        for message_index, block_replacements in replacements.items():
            message = messages[message_index]
            blocks = list(message.content)
            for block_index, replacement in block_replacements.items():
                blocks[block_index] = replacement
            messages[message_index] = replace(message, content=tuple(blocks))
        pruned_messages = tuple(messages)
        before_tokens = self._total_tokens(
            compiled.system_prompt,
            compiled.messages,
            compiled.budget,
        )
        after_tokens = self._total_tokens(
            compiled.system_prompt,
            pruned_messages,
            compiled.budget,
        )
        reclaimed_tokens = max(0, before_tokens - after_tokens)
        if reclaimed_tokens < policy.min_reclaim_tokens(compiled.budget.input_capacity):
            return compiled
        return replace(
            compiled,
            messages=pruned_messages,
            estimated_tokens=after_tokens,
            pruning=ToolResultPruningRecord(
                tool_call_ids=tuple(tool_call_ids),
                artifact_ids=tuple(dict.fromkeys(artifact_ids)),
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                reclaimed_tokens=reclaimed_tokens,
                trigger=trigger,
            ),
        )

    @staticmethod
    def _recoverable_artifact(
        block: ToolResultBlock,
        *,
        artifact_store: ArtifactStore,
        artifact_access: ArtifactAccess,
    ) -> ArtifactRef | None:
        artifact_id = block.metadata.get("artifact_id")
        expected_sha = block.metadata.get("artifact_sha256")
        expected_size = block.metadata.get("artifact_size_bytes")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(expected_sha, str)
            or not expected_sha
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            return None
        try:
            ref = artifact_store.get_ref(artifact_id, access=artifact_access)
            artifact_store.read_text(artifact_id, access=artifact_access)
        except Exception:  # noqa: BLE001 - an optional context edit must fail closed.
            return None
        if ref.sha256 != expected_sha or ref.size_bytes != expected_size:
            return None
        return ref

    async def compact_context_state(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        tools: tuple[ToolSpec, ...],
        budget: ContextBudget,
        policy: CompactionPolicy | None = None,
        events: tuple[Event, ...] | list[Event] = (),
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        known_artifacts: tuple[ArtifactRef, ...] = (),
        sections: tuple[ContextSection, ...] = (),
        summary_generator: SummaryGenerator | None = None,
        trigger: str = "hard_threshold",
    ) -> CompiledContext:
        """Create an evidence-backed cumulative state plus a recent raw tail."""

        resolved_policy = policy or CompactionPolicy()
        original = tuple(messages)
        system_blocks = compose_system_blocks(system_prompt, sections)
        system_prompt = "\n\n".join(block.text for block in system_blocks)
        materialized, discovered_artifacts = self._artifactize(
            original,
            artifact_store,
            artifact_policy,
        )
        artifacts = tuple(
            {
                artifact.artifact_id: artifact
                for artifact in (*known_artifacts, *discovered_artifacts)
            }.values()
        )
        before_tokens = self._full_input_tokens(system_prompt, materialized, budget)
        previous, state_message_ids = _previous_context_state(materialized)
        raw_messages = tuple(
            message for message in materialized if message.id not in state_message_ids
        )
        groups = _message_groups(raw_messages)
        if len(groups) <= resolved_policy.recent_tail_min_groups and not state_message_ids:
            raise ContextWindowExceeded(
                "Context cannot be reduced while preserving the minimum recent raw tail",
                details={
                    "estimated_tokens": before_tokens,
                    "target_tokens": int(
                        budget.input_capacity * resolved_policy.target_ratio
                    ),
                    "recent_tail_min_groups": resolved_policy.recent_tail_min_groups,
                },
            )

        protected = _recent_group_indexes(groups, budget.input_capacity, resolved_policy)
        retained_groups = [
            group for index, group in enumerate(groups) if index in protected
        ]
        omitted_groups = [
            group for index, group in enumerate(groups) if index not in protected
        ]
        if not omitted_groups and len(retained_groups) > resolved_policy.recent_tail_min_groups:
            omitted_groups.append(retained_groups.pop(0))
        previous_compaction_id = _previous_compaction_event_id(events, state_message_ids)
        generator = summary_generator or self.summary_generator
        fallback_reason: str | None = None
        target_tokens = int(budget.input_capacity * resolved_policy.target_ratio)

        while True:
            omitted = tuple(message for group in omitted_groups for message in group)
            retained = tuple(message for group in retained_groups for message in group)
            try:
                enrichment = await generator.generate(
                    SummaryRequest(
                        omitted_messages=omitted,
                        retained_messages=retained,
                        system_prompt=system_prompt,
                        artifact_ids=tuple(ref.artifact_id for ref in artifacts),
                    )
                )
            except Exception as error:  # noqa: BLE001 - semantic enrichment is optional.
                fallback_reason = type(error).__name__
                enrichment = await DeterministicSummaryGenerator().generate(
                    SummaryRequest(
                        omitted_messages=omitted,
                        retained_messages=retained,
                        system_prompt=system_prompt,
                        artifact_ids=tuple(ref.artifact_id for ref in artifacts),
                    )
                )
                enrichment = replace(enrichment, strategy="l2_model_fallback")

            strategy = _context_state_strategy(enrichment.strategy)
            state = project_context_state(
                messages=raw_messages,
                events=events,
                tools=tools,
                artifacts=artifacts,
                previous=previous,
                enrichment=enrichment,
                enrichment_source_message_ids=tuple(message.id for message in omitted),
                previous_compaction_id=previous_compaction_id,
                omitted_message_count=len(omitted),
                strategy=strategy,
            )
            summary_message = state.to_message(strategy=strategy)
            candidate = (summary_message, *retained)
            after_tokens = self._full_input_tokens(system_prompt, candidate, budget)
            if after_tokens <= target_tokens:
                break
            if len(retained_groups) <= resolved_policy.recent_tail_min_groups:
                raise ContextWindowExceeded(
                    "ContextState and the minimum recent raw tail exceed the compaction target",
                    details={
                        "estimated_tokens": after_tokens,
                        "target_tokens": target_tokens,
                        "recent_tail_groups": len(retained_groups),
                    },
                )
            omitted_groups.append(retained_groups.pop(0))
            omitted_groups.sort(key=lambda group: raw_messages.index(group[0]))

        replaced_ids = (
            *state_message_ids,
            *(message.id for group in omitted_groups for message in group),
        )
        stable_hash = _stable_prefix_hash(system_blocks, tools)
        record = CompactionRecord(
            strategy=strategy,
            version=2,
            replaced_message_ids=tuple(replaced_ids),
            retained_message_ids=tuple(message.id for message in retained),
            summary=summary_message,
            context_state=state,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            artifact_ids=tuple(ref.artifact_id for ref in artifacts),
            prompt_hash=_prompt_hash(system_prompt, materialized, tools, budget),
            trigger=trigger,
            source_message_ids=state.source_message_ids,
            source_event_seqs=state.source_event_seqs,
            policy_snapshot=asdict(resolved_policy),
            stable_prefix_hash=stable_hash,
            cache_epoch_hash=stable_hash,
            summary_generator=type(generator).__name__,
            fallback_reason=(
                fallback_reason
                or ("model_summary_invalid" if enrichment.strategy == "l2_model_fallback" else None)
            ),
        )
        return CompiledContext(
            system_prompt=system_prompt,
            messages=candidate,
            estimated_tokens=after_tokens,
            budget=budget,
            system_blocks=system_blocks,
            artifacts=artifacts,
            compaction=record,
        )

    async def compact_l2(
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
        system_blocks = compose_system_blocks(system_prompt, sections)
        system_prompt = "\n\n".join(block.text for block in system_blocks)
        materialized, artifacts = self._artifactize(original, artifact_store, artifact_policy)
        before_tokens = self._total_tokens(system_prompt, materialized, budget)
        groups = _message_groups(materialized)
        if len(groups) < 2:
            raise ContextWindowExceeded(
                "Context cannot be reduced because it has fewer than two message groups",
                details={
                    "estimated_tokens": before_tokens,
                    "usable_input": budget.usable_input,
                },
            )

        message_limit = max(1, budget.usable_input - estimate_text_tokens(system_prompt))
        generator = summary_generator or self.summary_generator
        omitted = tuple(message for group in groups[:-1] for message in group)
        retained = groups[-1]
        summary = await generator.generate(
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
                details={
                    "estimated_tokens": before_tokens,
                    "usable_input": budget.usable_input,
                },
            )
        record = CompactionRecord(
            # The record names the generator that actually produced the summary,
            # so a fallback from a compact model is visible in the event log.
            strategy=summary.strategy,
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
            system_blocks=system_blocks,
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

    @staticmethod
    def _full_input_tokens(
        system_prompt: str,
        messages: tuple[Message, ...],
        budget: ContextBudget,
    ) -> int:
        return (
            estimate_text_tokens(system_prompt)
            + budget.tool_schema_tokens
            + estimate_messages_tokens(messages)
        )


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


def _recent_group_indexes(
    groups: list[tuple[Message, ...]],
    input_capacity: int,
    policy: CompactionPolicy,
) -> set[int]:
    protected: set[int] = set()
    retained_tokens = 0
    recent_budget = policy.recent_tail_budget(input_capacity)
    for group_index in range(len(groups) - 1, -1, -1):
        group_tokens = estimate_messages_tokens(groups[group_index])
        if (
            len(protected) < policy.recent_tail_min_groups
            or retained_tokens + group_tokens <= recent_budget
        ):
            protected.add(group_index)
            retained_tokens += group_tokens
            continue
        break
    return protected


def _tool_result_placeholder(tool_name: str, artifact: ArtifactRef) -> str:
    return "\n".join(
        (
            "[tool result body removed from active context]",
            f"tool={tool_name}",
            f"artifact_id={artifact.artifact_id}",
            f"sha256={artifact.sha256}",
            f"size_bytes={artifact.size_bytes}",
            "summary=Tool result retained as a recoverable artifact.",
        )
    )


def _previous_context_state(
    messages: tuple[Message, ...],
) -> tuple[ContextState | None, tuple[str, ...]]:
    previous: ContextState | None = None
    state_message_ids: list[str] = []
    for message in messages:
        candidate: ContextState | None = None
        if message.metadata.get("context_state_schema_version") == 2:
            try:
                candidate = ContextState.from_message(message)
            except ValueError:
                candidate = None
        if candidate is None:
            candidate = legacy_summary_state(message)
        if candidate is not None:
            previous = candidate
            state_message_ids.append(message.id)
    return previous, tuple(state_message_ids)


def _previous_compaction_event_id(
    events: tuple[Event, ...] | list[Event],
    state_message_ids: tuple[str, ...],
) -> str | None:
    candidates = set(state_message_ids)
    for event in reversed(events):
        if event.type != "compaction.created":
            continue
        raw = event.data.get("summary")
        if isinstance(raw, dict) and raw.get("id") in candidates:
            return event.id
    return None


def _context_state_strategy(summary_strategy: str) -> str:
    if summary_strategy == "l2_model":
        return "l2_model_context_state"
    if summary_strategy == "l2_model_fallback":
        return "l2_model_fallback"
    return "l2_context_state"


def _stable_prefix_hash(
    system_blocks: tuple[TextBlock, ...],
    tools: tuple[ToolSpec, ...],
) -> str:
    stable_blocks: list[str] = []
    for block in system_blocks:
        if not block.stable_prefix:
            break
        stable_blocks.append(block.text)
    material = json.dumps(
        {
            "system_blocks": stable_blocks,
            "tools": sorted(
                (tool.model_definition.to_dict() for tool in tools),
                key=lambda item: str(item["name"]),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


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
