"""Public facade for context assembly and rolling compaction."""

from __future__ import annotations

from aihi.agent._core.events import Event
from aihi.agent.artifacts import ArtifactPolicy, ArtifactRef, ArtifactStore
from aihi.agent.context.assembler import (
    ContextAssembler,
    compose_system_blocks,
    compose_system_prompt,
)
from aihi.agent.context.compaction import ContextCompactor, EventReader
from aihi.agent.context.models import (
    AssembledContext,
    CompactionRecord,
    CompiledContext,
    ContextBudget,
    ContextSection,
)
from aihi.agent.context.policy import CompactionPolicy
from aihi.agent.context.summary import SummaryGenerator
from aihi.agent.tools.spec import ToolSpec
from aihi.models import Message


class ContextCompiler:
    """Coordinate the two context phases without owning runtime persistence.

    ``compile`` is deterministic and side-effect free apart from the injected
    artifact store. ``compact`` is the only semantic editing operation. The
    coordinator decides *when* it is needed from a measured ModelRequest.
    """

    def __init__(
        self,
        *,
        artifact_threshold_tokens: int = 1_024,
        artifact_preview_chars: int = 4_000,
        summary_generator: SummaryGenerator | None = None,
    ) -> None:
        self.assembler = ContextAssembler(
            artifact_threshold_tokens=artifact_threshold_tokens,
            artifact_preview_chars=artifact_preview_chars,
        )
        self.compactor = ContextCompactor(summary_generator=summary_generator)

    def compile(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        budget: ContextBudget,
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        sections: tuple[ContextSection, ...] = (),
        known_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> CompiledContext:
        assembled = self.assembler.assemble(
            messages,
            system_prompt=system_prompt,
            budget=budget,
            sections=sections,
            artifact_store=artifact_store,
            artifact_policy=artifact_policy,
            known_artifacts=known_artifacts,
        )
        return CompiledContext(
            system_prompt=assembled.system_prompt,
            messages=assembled.messages,
            estimated_tokens=assembled.estimated_tokens,
            budget=assembled.budget,
            system_blocks=assembled.system_blocks,
            artifacts=assembled.artifacts,
        )

    async def compact(
        self,
        compiled: CompiledContext,
        *,
        tools: tuple[ToolSpec, ...],
        policy: CompactionPolicy,
        events: tuple[Event, ...] | list[Event] = (),
        event_reader: EventReader | None = None,
        summary_generator: SummaryGenerator | None = None,
        trigger: str = "threshold",
    ) -> CompiledContext:
        assembled = AssembledContext(
            system_prompt=compiled.system_prompt,
            system_blocks=compiled.system_blocks,
            messages=compiled.messages,
            artifacts=compiled.artifacts,
            estimated_tokens=compiled.estimated_tokens,
            budget=compiled.budget,
        )
        return await self.compactor.compact(
            assembled,
            tools=tools,
            policy=policy,
            events=events,
            event_reader=event_reader,
            summary_generator=summary_generator,
            trigger=trigger,
        )

    async def compile_and_compact(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        tools: tuple[ToolSpec, ...],
        budget: ContextBudget,
        policy: CompactionPolicy | None = None,
        events: tuple[Event, ...] | list[Event] = (),
        event_reader: EventReader | None = None,
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        known_artifacts: tuple[ArtifactRef, ...] = (),
        sections: tuple[ContextSection, ...] = (),
        summary_generator: SummaryGenerator | None = None,
        trigger: str = "threshold",
    ) -> CompiledContext:
        """Build and compact in one call for adapters that need a one-shot API."""

        compiled = self.compile(
            messages,
            system_prompt=system_prompt,
            budget=budget,
            artifact_store=artifact_store,
            artifact_policy=artifact_policy,
            sections=sections,
            known_artifacts=known_artifacts,
        )
        return await self.compact(
            compiled,
            tools=tools,
            policy=policy or CompactionPolicy(),
            events=events,
            event_reader=event_reader,
            summary_generator=summary_generator,
            trigger=trigger,
        )


__all__ = [
    "AssembledContext",
    "CompactionRecord",
    "CompiledContext",
    "ContextBudget",
    "ContextCompiler",
    "ContextSection",
    "compose_system_blocks",
    "compose_system_prompt",
]
