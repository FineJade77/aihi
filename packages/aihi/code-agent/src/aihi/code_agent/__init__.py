"""Coding Agent domain runtime for AIHI."""

from typing import Any

from aihi.code_agent.config import CodeAgentConfig, CodeAgentConfigError, load_config
from aihi.code_agent.evals import (
    CodeAgentEvalRunner,
    CodeEvalGateFailed,
    CodeEvalReport,
    CodeEvalValidationError,
    CodeTask,
    CodeTaskDataset,
    CodeTaskResult,
    CommandOutcome,
    PreparedWorkspace,
    TaskExecution,
    WorkspaceManager,
    directory_sha256,
)
from aihi.code_agent.framing import FrameError, read_frame, write_frame
from aihi.code_agent.protocol import (
    COMMAND_DESCRIPTORS,
    PROTOCOL_VERSION,
    SERVER_NAME,
)
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.code_agent.turns import (
    ApprovalRequested,
    AssistantMessage,
    RunStateChanged,
    SubagentCompleted,
    SubagentSpawned,
    SubagentStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
)


def __getattr__(name: str) -> Any:
    """Load the executable Worker module lazily so ``python -m`` stays quiet."""

    if name in {"WorkerServer", "main", "serve_stdio"}:
        from aihi.code_agent.worker import WorkerServer, main, serve_stdio

        if name == "WorkerServer":
            return WorkerServer
        return main if name == "main" else serve_stdio
    raise AttributeError(name)

__all__ = [
    "COMMAND_DESCRIPTORS",
    "ApprovalRequested",
    "AssistantMessage",
    "CodeAgentEvalRunner",
    "CodeAgentConfig",
    "CodeAgentConfigError",
    "CodeAgentRuntime",
    "CodeEvalGateFailed",
    "CodeEvalReport",
    "CodeEvalValidationError",
    "CodeTask",
    "CodeTaskDataset",
    "CodeTaskResult",
    "CommandOutcome",
    "FrameError",
    "PreparedWorkspace",
    "PROTOCOL_VERSION",
    "RunStateChanged",
    "SERVER_NAME",
    "SubagentCompleted",
    "SubagentSpawned",
    "SubagentStarted",
    "TextDelta",
    "TaskExecution",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnEvent",
    "TurnFinished",
    "WorkspaceManager",
    "directory_sha256",
    "WorkerServer",
    "main",
    "load_config",
    "read_frame",
    "serve_stdio",
    "write_frame",
]
