"""Offline trace replay, datasets, and deterministic evaluation graders."""

from aihi.agent.evals.conformance import (
    HarnessConformanceCase,
    HarnessConformanceCaseResult,
    HarnessConformanceReport,
    HarnessConformanceRunner,
)
from aihi.agent.evals.dataset import EvalCase, EvalDataset
from aihi.agent.evals.errors import (
    EvalError,
    EvalValidationError,
    ReplayInvariantViolation,
)
from aihi.agent.evals.golden import GoldenTask, GoldenTaskGrader
from aihi.agent.evals.graders import (
    CompositeGrader,
    EventCountGrader,
    Grade,
    Grader,
    RunStateGrader,
)
from aihi.agent.evals.replay import ReplayEngine, ReplayResult, TraceBundle
from aihi.agent.evals.runner import EvalCaseResult, EvalRunner
from aihi.agent.evals.trace_graph import (
    Delegation,
    GraphReplayResult,
    TraceGraph,
    replay_graph,
)

__all__ = [
    "CompositeGrader",
    "Delegation",
    "EvalCase",
    "EvalCaseResult",
    "EvalDataset",
    "EvalError",
    "EvalRunner",
    "EvalValidationError",
    "EventCountGrader",
    "GoldenTask",
    "GoldenTaskGrader",
    "Grade",
    "Grader",
    "GraphReplayResult",
    "HarnessConformanceCase",
    "HarnessConformanceCaseResult",
    "HarnessConformanceReport",
    "HarnessConformanceRunner",
    "ProviderGoldenCase",
    "ProviderGoldenResult",
    "ReplayEngine",
    "ReplayInvariantViolation",
    "ReplayResult",
    "RunStateGrader",
    "TraceBundle",
    "TraceGraph",
    "normalize_chunk",
    "replay_graph",
    "request_fingerprint",
    "run_provider_golden",
]
