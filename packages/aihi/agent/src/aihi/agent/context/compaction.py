"""Rolling-summary compaction over closed Tool Call exchanges."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from hashlib import sha256
from typing import TypeAlias

from aihi.agent._core.errors import ContextWindowExceeded
from aihi.agent._core.events import Event
from aihi.agent.context.grouping import group_tool_exchanges
from aihi.agent.context.models import AssembledContext, CompactionRecord, CompiledContext
from aihi.agent.context.policy import CompactionPolicy
from aihi.agent.context.projector import legacy_summary_state, project_context_state
from aihi.agent.context.state import (
    CONTEXT_STATE_SCHEMA_VERSION,
    FACT_FIELDS,
    ContextFact,
    ContextState,
)
from aihi.agent.context.summary import (
    DeterministicSummaryGenerator,
    StructuredSummary,
    SummaryGenerator,
    SummaryRequest,
)
from aihi.agent.tools.spec import ToolSpec
from aihi.models import Message, TextBlock, estimate_messages_tokens, estimate_text_tokens

EventReader: TypeAlias = Callable[[int], tuple[Event, ...] | list[Event]]

# Bounded retention drops the cheapest evidence first, not simply the oldest.
# A tool receipt can be re-derived from the durable Event log, so it is the
# right thing to lose. A model-authored semantic fact cannot: once its source
# messages have left the context no later summary can produce it again. A
# pending approval is live state that a run is currently blocked on, so it goes
# last. Ordering only by age would evict exactly the wrong tier, because a fact
# is anchored to the sequence it was observed at and is never refreshed, while
# receipts keep arriving with newer sequences.
_EVICTION_TIERS: tuple[tuple[str, ...], ...] = (
    ("failures", "verified", "files", "subagents", "skills"),
    ("constraints", "decisions", "open_questions", "next_steps"),
    ("pending_approvals",),
)

_TIERED_FIELDS = tuple(field for tier in _EVICTION_TIERS for field in tier)
if sorted(_TIERED_FIELDS) != sorted(FACT_FIELDS):
    raise RuntimeError(
        "_EVICTION_TIERS must rank every ContextState fact field exactly once"
    )


class ContextCompactor:
    """Replace old closed exchanges with one cumulative ContextState."""

    def __init__(self, summary_generator: SummaryGenerator | None = None) -> None:
        self.summary_generator = summary_generator or DeterministicSummaryGenerator()

    async def compact(
        self,
        assembled: AssembledContext,
        *,
        tools: tuple[ToolSpec, ...],
        policy: CompactionPolicy,
        events: tuple[Event, ...] | list[Event] = (),
        event_reader: EventReader | None = None,
        summary_generator: SummaryGenerator | None = None,
        trigger: str = "threshold",
    ) -> CompiledContext:
        previous, summary_ids = _previous_context_state(assembled.messages)
        raw_messages = tuple(
            message for message in assembled.messages if message.id not in summary_ids
        )
        groups = group_tool_exchanges(raw_messages)
        if len(groups) < 2:
            raise ContextWindowExceeded(
                "Context cannot be compacted without an older closed message group",
                details={
                    "estimated_tokens": assembled.estimated_tokens,
                    "input_capacity": assembled.budget.input_capacity,
                    "message_groups": len(groups),
                },
            )

        event_cursor = previous.event_cursor if previous is not None else 0
        event_delta = _read_event_delta(events, event_reader, event_cursor)
        retained_indexes = _initial_tail_indexes(groups, assembled, policy)
        if len(retained_indexes) == len(groups):
            retained_indexes.remove(min(retained_indexes))

        generator = summary_generator or self.summary_generator
        target_tokens = int(
            assembled.budget.input_capacity * policy.compaction_target_ratio
        )
        fallback_reason: str | None = None

        while True:
            retained = tuple(
                message
                for index in sorted(retained_indexes)
                for message in groups[index]
            )
            omitted = tuple(
                message
                for index, group in enumerate(groups)
                if index not in retained_indexes
                for message in group
            )
            enrichment, generator_error = await _generate_summary(
                generator,
                SummaryRequest(
                    omitted_messages=omitted,
                    retained_messages=retained,
                    system_prompt=assembled.system_prompt,
                    artifact_ids=tuple(ref.artifact_id for ref in assembled.artifacts),
                ),
            )
            fallback_reason = generator_error or enrichment.fallback_reason
            state = project_context_state(
                # The first state snapshots all current evidence. Later states
                # trust the bounded prior and apply only events after its cursor.
                messages=raw_messages if previous is None else (),
                objective_messages=raw_messages,
                events=event_delta,
                tools=tools,
                artifacts=assembled.artifacts,
                previous=previous,
                enrichment=enrichment,
                enrichment_source_message_ids=tuple(message.id for message in omitted),
                previous_compaction_id=_previous_compaction_event_id(
                    event_delta, summary_ids
                ),
                omitted_message_count=len(omitted),
                strategy=_state_strategy(enrichment.strategy),
            )
            state = _bound_state(state, policy)
            strategy = _state_strategy(enrichment.strategy)
            summary_message = state.to_message(strategy=strategy)
            candidate = (summary_message, *retained)
            after_tokens = _full_tokens(assembled.system_prompt, candidate, assembled)
            if after_tokens <= target_tokens:
                break
            if len(retained_indexes) == 1:
                if after_tokens <= assembled.budget.input_capacity:
                    break
                raise ContextWindowExceeded(
                    "Context summary and the newest closed group exceed the input budget",
                    details={
                        "estimated_tokens": after_tokens,
                        "target_tokens": target_tokens,
                        "retained_groups": 1,
                    },
                )
            retained_indexes.remove(min(retained_indexes))

        stable_hash = _stable_prefix_hash(assembled.system_blocks, tools)
        record = CompactionRecord(
            strategy=strategy,
            version=2,
            replaced_message_ids=tuple(
                (*summary_ids, *(message.id for message in omitted))
            ),
            retained_message_ids=tuple(message.id for message in retained),
            summary=summary_message,
            context_state=state,
            before_tokens=assembled.estimated_tokens,
            after_tokens=after_tokens,
            artifact_ids=tuple(ref.artifact_id for ref in assembled.artifacts),
            prompt_hash=_prompt_hash(assembled, tools),
            trigger=trigger,
            source_message_ids=state.source_message_ids,
            source_event_seqs=state.source_event_seqs,
            policy_snapshot=asdict(policy),
            stable_prefix_hash=stable_hash,
            summary_generator=type(generator).__name__,
            fallback_reason=fallback_reason,
        )
        return CompiledContext(
            system_prompt=assembled.system_prompt,
            messages=candidate,
            estimated_tokens=after_tokens,
            budget=assembled.budget,
            system_blocks=assembled.system_blocks,
            artifacts=assembled.artifacts,
            compaction=record,
        )


async def _generate_summary(
    generator: SummaryGenerator,
    request: SummaryRequest,
) -> tuple[StructuredSummary, str | None]:
    try:
        return await generator.generate(request), None
    except Exception as error:  # noqa: BLE001 - compaction must degrade safely.
        fallback = await DeterministicSummaryGenerator().generate(request)
        return (
            replace(
                fallback,
                strategy="rolling_summary_fallback",
                fallback_reason=type(error).__name__,
            ),
            type(error).__name__,
        )


def _read_event_delta(
    events: tuple[Event, ...] | list[Event],
    event_reader: EventReader | None,
    after_seq: int,
) -> tuple[Event, ...]:
    if event_reader is not None:
        return tuple(event_reader(after_seq))
    return tuple(
        event
        for event in events
        if event.seq is None or event.seq > after_seq
    )


def _initial_tail_indexes(
    groups: tuple[tuple[Message, ...], ...],
    assembled: AssembledContext,
    policy: CompactionPolicy,
) -> set[int]:
    target_tokens = int(
        assembled.budget.input_capacity * policy.compaction_target_ratio
    )
    fixed_tokens = (
        estimate_text_tokens(assembled.system_prompt)
        + assembled.budget.tool_schema_tokens
    )
    # Message framing makes a JSON ContextState larger than its raw text.
    summary_reserve = int(policy.summary_max_tokens * 4 / 3) + 8
    target_tail_budget = max(0, target_tokens - fixed_tokens - summary_reserve)
    tail_budget = min(
        policy.recent_tail_budget(assembled.budget.input_capacity),
        target_tail_budget,
    )
    selected: set[int] = set()
    tokens = 0
    for index in range(len(groups) - 1, -1, -1):
        group_tokens = estimate_messages_tokens(groups[index])
        if not selected or tokens + group_tokens <= tail_budget:
            selected.add(index)
            tokens += group_tokens
            continue
        break
    return selected


def _full_tokens(
    system_prompt: str,
    messages: tuple[Message, ...],
    assembled: AssembledContext,
) -> int:
    return (
        estimate_text_tokens(system_prompt)
        + assembled.budget.tool_schema_tokens
        + estimate_messages_tokens(messages)
    )


def _previous_context_state(
    messages: tuple[Message, ...],
) -> tuple[ContextState | None, tuple[str, ...]]:
    previous: ContextState | None = None
    state_ids: list[str] = []
    for message in messages:
        candidate: ContextState | None = None
        if (
            message.metadata.get("context_state_schema_version")
            == CONTEXT_STATE_SCHEMA_VERSION
        ):
            try:
                candidate = ContextState.from_message(message)
            except ValueError:
                candidate = None
        if candidate is None:
            candidate = legacy_summary_state(message)
        if candidate is not None:
            previous = candidate
            state_ids.append(message.id)
    return previous, tuple(state_ids)


def _previous_compaction_event_id(
    events: tuple[Event, ...],
    state_ids: tuple[str, ...],
) -> str | None:
    wanted = set(state_ids)
    for event in reversed(events):
        if event.type != "compaction.created":
            continue
        summary = event.data.get("summary")
        if isinstance(summary, dict) and summary.get("id") in wanted:
            return event.id
    return None


def _state_strategy(summary_strategy: str) -> str:
    if summary_strategy == "rolling_summary_fallback":
        return "rolling_summary_fallback"
    if summary_strategy == "rolling_summary_model":
        return "rolling_summary_model"
    return "rolling_summary"


def _bound_state(state: ContextState, policy: CompactionPolicy) -> ContextState:
    """Bound cumulative state by recency without replaying discarded facts."""

    def clipped(field: str) -> tuple[ContextFact, ...]:
        return tuple(
            sorted(
                (
                    replace(item, text=item.text[: policy.summary_fact_max_chars])
                    for item in getattr(state, field)
                ),
                key=_fact_recency,
            )
        )[-64:]

    bounded = replace(
        state,
        objective=state.objective[: policy.summary_fact_max_chars],
        source_message_ids=state.source_message_ids[-128:],
        source_event_seqs=state.source_event_seqs[-128:],
        constraints=clipped("constraints"),
        decisions=clipped("decisions"),
        files=clipped("files"),
        verified=clipped("verified"),
        failures=clipped("failures"),
        open_questions=clipped("open_questions"),
        next_steps=clipped("next_steps"),
        pending_approvals=clipped("pending_approvals"),
        skills=clipped("skills"),
        subagents=clipped("subagents"),
    )
    while (
        estimate_text_tokens(json.dumps(bounded.to_dict(), ensure_ascii=False))
        > policy.summary_max_tokens
    ):
        candidates = [
            (tier, _fact_recency(getattr(bounded, field)[0]), index, field)
            for tier, fields in enumerate(_EVICTION_TIERS)
            for index, field in enumerate(fields)
            if getattr(bounded, field)
        ]
        if not candidates:
            break
        # Cheapest tier first, then the oldest fact inside it; the field index
        # only breaks exact ties so the choice stays deterministic.
        *_, evicted_field = min(candidates)
        values = getattr(bounded, evicted_field)
        bounded = replace(bounded, **{evicted_field: values[1:]})
    return bounded


def _fact_recency(fact: ContextFact) -> int:
    if fact.observed_seq is not None:
        return fact.observed_seq
    return max(fact.source_event_seqs, default=-1)


def _stable_prefix_hash(
    system_blocks: tuple[TextBlock, ...],
    tools: tuple[ToolSpec, ...],
) -> str:
    stable: list[str] = []
    for block in system_blocks:
        if not block.stable_prefix:
            break
        stable.append(block.text)
    material = json.dumps(
        {
            "system_blocks": stable,
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


def _prompt_hash(assembled: AssembledContext, tools: tuple[ToolSpec, ...]) -> str:
    material = json.dumps(
        {
            "system_prompt": assembled.system_prompt,
            "messages": [message.to_dict() for message in assembled.messages],
            "tools": [tool.model_definition.to_dict() for tool in tools],
            "input_capacity": assembled.budget.input_capacity,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


__all__ = ["ContextCompactor", "EventReader"]
