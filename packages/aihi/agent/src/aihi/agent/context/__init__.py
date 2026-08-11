"""Context compilation, deterministic compaction, and L2 summary contracts."""

from aihi.agent.context.compiler import (
    CompactionRecord,
    CompiledContext,
    ContextBudget,
    ContextCompiler,
    ContextSection,
    compose_system_prompt,
)
from aihi.agent.context.model_summary import (
    STRATEGY_FALLBACK,
    STRATEGY_MODEL,
    ModelSummaryGenerator,
)
from aihi.agent.context.summary import (
    DeterministicSummaryGenerator,
    StructuredSummary,
    SummaryGenerator,
    SummaryRequest,
)

__all__ = [
    "CompactionRecord",
    "CompiledContext",
    "ContextBudget",
    "ContextCompiler",
    "ContextSection",
    "DeterministicSummaryGenerator",
    "ModelSummaryGenerator",
    "STRATEGY_FALLBACK",
    "STRATEGY_MODEL",
    "StructuredSummary",
    "SummaryGenerator",
    "SummaryRequest",
    "compose_system_prompt",
]
