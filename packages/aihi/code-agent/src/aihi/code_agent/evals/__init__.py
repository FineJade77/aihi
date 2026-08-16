"""Coding Agent task datasets, runners, workspaces and reports."""

from aihi.code_agent.evals.dataset import (
    CodeEvalValidationError,
    CodeTask,
    CodeTaskDataset,
    directory_sha256,
)
from aihi.code_agent.evals.graders import (
    CommandOutcome,
    average_grade,
    grade_commands,
    grade_expected_files,
    grade_harness_trace,
    grade_scope,
)
from aihi.code_agent.evals.report import (
    CodeEvalGateFailed,
    CodeEvalReport,
    CodeTaskResult,
)
from aihi.code_agent.evals.runner import (
    CodeAgentEvalRunner,
    CommandExecutor,
    TaskExecution,
    TaskExecutor,
)
from aihi.code_agent.evals.workspace import (
    PreparedWorkspace,
    WorkspaceManager,
    changed_paths,
    paths_match,
    snapshot_files,
)

__all__ = [
    "CodeAgentEvalRunner",
    "CodeEvalGateFailed",
    "CodeEvalReport",
    "CodeEvalValidationError",
    "CodeTask",
    "CodeTaskDataset",
    "CodeTaskResult",
    "CommandExecutor",
    "CommandOutcome",
    "PreparedWorkspace",
    "TaskExecution",
    "TaskExecutor",
    "WorkspaceManager",
    "average_grade",
    "changed_paths",
    "directory_sha256",
    "grade_commands",
    "grade_expected_files",
    "grade_harness_trace",
    "grade_scope",
    "paths_match",
    "snapshot_files",
]
