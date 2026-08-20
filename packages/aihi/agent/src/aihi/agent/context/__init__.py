"""Context compilation, deterministic compaction, and L2 summary contracts."""

from aihi.agent.context.cache import (
    PROMPT_CACHE_CONTRACT_VERSION,
    build_prompt_cache_key,
    stable_system_blocks,
)
from aihi.agent.context.compiler import (
    CompactionRecord,
    CompiledContext,
    ContextBudget,
    ContextCompiler,
    ContextSection,
    compose_system_blocks,
    compose_system_prompt,
)
from aihi.agent.context.model_summary import (
    STRATEGY_FALLBACK,
    STRATEGY_MODEL,
    ModelSummaryGenerator,
)
from aihi.agent.context.policy import (
    CompactionPolicy,
    ContextPressure,
    ContextPressureController,
)
from aihi.agent.context.state import (
    CONTEXT_STATE_SCHEMA_VERSION,
    ArtifactState,
    ContextFact,
    ContextState,
)
from aihi.agent.context.summary import (
    DeterministicSummaryGenerator,
    StructuredSummary,
    SummaryGenerator,
    SummaryRequest,
)

__all__ = [
    "ArtifactState",
    "CONTEXT_STATE_SCHEMA_VERSION",
    "CompactionRecord",
    "CompactionPolicy",
    "CompiledContext",
    "ContextBudget",
    "ContextCompiler",
    "ContextFact",
    "ContextPressure",
    "ContextPressureController",
    "ContextSection",
    "ContextState",
    "DeterministicSummaryGenerator",
    "ModelSummaryGenerator",
    "PROMPT_CACHE_CONTRACT_VERSION",
    "STRATEGY_FALLBACK",
    "STRATEGY_MODEL",
    "StructuredSummary",
    "SummaryGenerator",
    "SummaryRequest",
    "build_prompt_cache_key",
    "compose_system_blocks",
    "compose_system_prompt",
    "stable_system_blocks",
]
