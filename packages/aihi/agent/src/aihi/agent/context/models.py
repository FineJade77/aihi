"""Value objects shared by context assembly and compaction.

The context package deliberately keeps these records independent from the
runtime coordinator.  A compiled context is a model-input projection; it is
not session state and it is never the source of truth for a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from aihi.agent.artifacts import ArtifactRef
from aihi.agent.context.policy import ContextPressure
from aihi.agent.context.state import ContextState
from aihi.agent.tools.spec import ToolSpec
from aihi.models import MESSAGE_SCHEMA_VERSION, Message, TextBlock


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """The complete request budget after output and safety reservations."""

    context_window: int
    reserved_output: int
    tool_schema_tokens: int = 0
    safety_margin: int = 256

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.reserved_output < 0 or self.tool_schema_tokens < 0 or self.safety_margin < 0:
            raise ValueError("Context budget components cannot be negative")
        if self.input_capacity <= 0 or self.usable_input <= 0:
            raise ValueError("Context budget leaves no usable input capacity")

    @property
    def input_capacity(self) -> int:
        """Maximum prompt tokens including the tool schema."""

        return self.context_window - self.reserved_output - self.safety_margin

    @property
    def usable_input(self) -> int:
        """Maximum system+message tokens after reserving tool definitions."""

        return self.input_capacity - self.tool_schema_tokens

    @classmethod
    def for_request(
        cls,
        *,
        context_window: int,
        reserved_output: int,
        tools: tuple[ToolSpec, ...] = (),
        safety_margin: int = 256,
    ) -> ContextBudget:
        tool_schema_tokens = _estimate_tool_schema_tokens(tools)
        return cls(
            context_window=context_window,
            reserved_output=reserved_output,
            tool_schema_tokens=tool_schema_tokens,
            safety_margin=safety_margin,
        )


@dataclass(frozen=True, slots=True)
class ContextSection:
    """A dynamic, application-provided section after the stable prefix."""

    title: str
    body: str
    source: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Context section title must not be empty")

    def render(self) -> str:
        return f"## {self.title.strip()}\n{self.body.strip()}"


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """One materialized model-input projection before pressure decisions."""

    system_prompt: str
    system_blocks: tuple[TextBlock, ...]
    messages: tuple[Message, ...]
    artifacts: tuple[ArtifactRef, ...]
    estimated_tokens: int
    budget: ContextBudget


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """Durable description of one rolling-summary replacement."""

    strategy: str
    version: int
    replaced_message_ids: tuple[str, ...]
    summary: Message
    before_tokens: int
    after_tokens: int
    artifact_ids: tuple[str, ...] = ()
    prompt_hash: str = ""
    trigger: str = "threshold"
    retained_message_ids: tuple[str, ...] = ()
    context_state: ContextState | None = None
    source_message_ids: tuple[str, ...] = ()
    source_event_seqs: tuple[int, ...] = ()
    policy_snapshot: dict[str, object] | None = None
    token_count_method: str = "estimate"
    stable_prefix_hash: str = ""
    summary_generator: str = ""
    fallback_reason: str | None = None

    def to_event_data(self) -> dict[str, object]:
        """Serialize the record without embedding the full raw transcript."""

        return {
            "strategy": self.strategy,
            "version": self.version,
            "strategy_version": self.version,
            "replaced_message_ids": list(self.replaced_message_ids),
            "summary": self.summary.to_dict(),
            "summary_message_schema_version": MESSAGE_SCHEMA_VERSION,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "artifact_ids": list(self.artifact_ids),
            "prompt_hash": self.prompt_hash,
            "trigger": self.trigger,
            "retained_message_ids": list(self.retained_message_ids),
            "context_state": self.context_state.to_dict() if self.context_state else None,
            "source_message_ids": list(self.source_message_ids),
            "source_event_seqs": list(self.source_event_seqs),
            "policy_snapshot": dict(self.policy_snapshot or {}),
            "token_count_method": self.token_count_method,
            "stable_prefix_hash": self.stable_prefix_hash,
            "summary_generator": self.summary_generator,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True, slots=True)
class CompiledContext:
    """Final model-input projection returned by :class:`ContextCompiler`."""

    system_prompt: str
    messages: tuple[Message, ...]
    estimated_tokens: int
    budget: ContextBudget
    system_blocks: tuple[TextBlock, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    compaction: CompactionRecord | None = None
    pressure: ContextPressure | None = None

    @property
    def over_budget(self) -> bool:
        return self.estimated_tokens > self.budget.input_capacity

def _estimate_tool_schema_tokens(tools: tuple[ToolSpec, ...]) -> int:
    from aihi.models import estimate_text_tokens

    return estimate_text_tokens(
        json.dumps(
            [tool.model_definition.to_dict() for tool in tools],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = [
    "AssembledContext",
    "CompactionRecord",
    "CompiledContext",
    "ContextBudget",
    "ContextSection",
]
