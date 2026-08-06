"""Governed subagent task graph and coordination primitives."""

from .coordinator import SubagentCoordinator
from .errors import (
    AgentBudgetExceeded,
    AgentDepthExceeded,
    AgentError,
    AgentPermissionDenied,
    AgentStateError,
    AgentValidationError,
    MailboxConflict,
    MailboxError,
)
from .graph import TaskGraph
from .mailbox import Mailbox, MailboxMessage
from .types import AgentBudget, AgentState, TaskNode, TaskResult, TaskSpec, WorkspaceScope
from .workspaces import PatchArtifact, WorktreePatchBoundary, WorktreeSpec

__all__ = [
    "AgentBudget",
    "AgentBudgetExceeded",
    "AgentDepthExceeded",
    "AgentError",
    "AgentPermissionDenied",
    "AgentState",
    "AgentStateError",
    "AgentValidationError",
    "Mailbox",
    "MailboxConflict",
    "MailboxError",
    "MailboxMessage",
    "SubagentCoordinator",
    "TaskGraph",
    "TaskNode",
    "TaskResult",
    "TaskSpec",
    "PatchArtifact",
    "WorktreePatchBoundary",
    "WorktreeSpec",
    "WorkspaceScope",
]
