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
    DockerCommandExecutor,
    TaskExecution,
    TaskExecutor,
    run_command_on_host,
)
from aihi.code_agent.evals.statistics import (
    CaseOutcome,
    RegressionVerdict,
    assess_regression,
    bootstrap_delta,
    collapsed_cases,
)
from aihi.code_agent.evals.workspace import (
    PreparedWorkspace,
    WorkspaceManager,
    changed_paths,
    paths_match,
    snapshot_files,
)

__all__ = [
    "CaseOutcome",
    "CodeAgentEvalRunner",
    "CodeEvalGateFailed",
    "CodeEvalReport",
    "CodeEvalValidationError",
    "CodeTask",
    "CodeTaskDataset",
    "CodeTaskResult",
    "CommandExecutor",
    "CommandOutcome",
    "DockerCommandExecutor",
    "PreparedWorkspace",
    "RegressionVerdict",
    "TaskExecution",
    "TaskExecutor",
    "WorkspaceManager",
    "assess_regression",
    "average_grade",
    "bootstrap_delta",
    "changed_paths",
    "collapsed_cases",
    "directory_sha256",
    "grade_commands",
    "grade_expected_files",
    "grade_harness_trace",
    "grade_scope",
    "paths_match",
    "run_command_on_host",
    "snapshot_files",
]
