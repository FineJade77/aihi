"""Offline trace replay, datasets, and deterministic evaluation graders."""

from aiharness.evals.dataset import EvalCase, EvalDataset
from aiharness.evals.errors import (
    EvalError,
    EvalGateFailed,
    EvalValidationError,
    ReplayInvariantViolation,
)
from aiharness.evals.gate import EvalGate, GateVerdict
from aiharness.evals.golden import GoldenTask, GoldenTaskGrader
from aiharness.evals.graders import (
    CompositeGrader,
    EventCountGrader,
    Grade,
    Grader,
    RunStateGrader,
)
from aiharness.evals.provider_golden import (
    ProviderGoldenCase,
    ProviderGoldenResult,
    ProviderGoldenRunner,
    ProviderGoldenTask,
    ProviderTranscript,
    normalize_chunk,
    request_fingerprint,
    run_provider_golden,
)
from aiharness.evals.replay import ReplayEngine, ReplayResult, TraceBundle
from aiharness.evals.runner import EvalCaseResult, EvalRunner
from aiharness.evals.trace_graph import (
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
    "EvalGate",
    "EvalGateFailed",
    "EvalRunner",
    "EvalValidationError",
    "EventCountGrader",
    "GateVerdict",
    "GoldenTask",
    "GoldenTaskGrader",
    "Grade",
    "Grader",
    "GraphReplayResult",
    "ProviderGoldenCase",
    "ProviderGoldenResult",
    "ProviderGoldenRunner",
    "ProviderGoldenTask",
    "ProviderTranscript",
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
