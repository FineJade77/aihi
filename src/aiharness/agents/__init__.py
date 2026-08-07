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
from .subagent import (
    SPAWN_CAPABILITY,
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentRunner,
    SubagentTool,
    restrict_registry,
    subagent_session_factory,
)
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
    "ChildRunSubagentRunner",
    "Mailbox",
    "MailboxConflict",
    "MailboxError",
    "MailboxMessage",
    "PatchArtifact",
    "SPAWN_CAPABILITY",
    "SubagentAuthority",
    "SubagentCoordinator",
    "SubagentRunner",
    "SubagentTool",
    "TaskGraph",
    "TaskNode",
    "TaskResult",
    "TaskSpec",
    "WorkspaceScope",
    "WorktreePatchBoundary",
    "WorktreeSpec",
    "restrict_registry",
    "subagent_session_factory",
]
