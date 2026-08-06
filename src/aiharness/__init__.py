"""AIHarness public composition API.

This module is the whole supported surface for applications (`aicode/`,
`personal/`, …). Everything an application needs to assemble a runtime is
re-exported here; anything reachable only through a submodule path is internal
and may change without an ADR.

Deliberately absent: `agents`, `memory`, `skills`, `plugins`, `mcp`, `evals`,
`api` and `cli`. Those packages exist but are not yet injectable into
`RunCoordinator`, so there is no composition contract to promise (TASK.md H-02).
They stay on explicit submodule imports until the Runtime wires them.

Importing this module must not require any optional extra (`fastapi`,
`psycopg`, `opentelemetry`); `tests/contract/test_public_api.py` enforces that.
"""

from aiharness.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactStore, FileArtifactStore
from aiharness.context import ContextCompiler
from aiharness.core.errors import (
    ContextWindowExceeded,
    EventInvariantViolation,
    HarnessError,
    SandboxViolation,
    SessionNotFound,
    ToolInputError,
    ToolNotFound,
    UnsafeHostNotAcknowledged,
)
from aiharness.core.events import Event
from aiharness.core.ids import new_id
from aiharness.core.types import (
    Capabilities,
    ContentBlock,
    Message,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
    Usage,
)
from aiharness.hooks import HookBus
from aiharness.models import ModelGateway, ModelRouter, Provider
from aiharness.models.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from aiharness.models.providers.fake import FakeProvider
from aiharness.observability import Telemetry
from aiharness.policy import (
    Approval,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResolver,
    CapabilityLease,
    Decision,
    DecisionEffect,
    DefaultPolicyEngine,
    PermissionContext,
    PermissionMode,
    PolicyEngine,
    StaticApprovalResolver,
    SuspendingApprovalResolver,
)
from aiharness.runtime import RunCoordinator, RunResult, RunState
from aiharness.sandbox import (
    DockerBackend,
    HostBackend,
    LocalIsolatedBackend,
    SandboxBackend,
    SandboxDescriptor,
)
from aiharness.sessions import EventStore, InMemoryEventStore, Session, SQLiteEventStore
from aiharness.tools import Tool, ToolContext, ToolRegistry, ToolResult
from aiharness.tools.builtin import (
    EditFileTool,
    ReadFileTool,
    RunTestsTool,
    ShellTool,
    WriteFileTool,
)

__all__ = [
    "AnthropicProvider",
    "Approval",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalResolver",
    "ArtifactAccess",
    "ArtifactPolicy",
    "ArtifactStore",
    "Capabilities",
    "CapabilityLease",
    "ContentBlock",
    "ContextCompiler",
    "ContextWindowExceeded",
    "Decision",
    "DecisionEffect",
    "DefaultPolicyEngine",
    "DockerBackend",
    "EditFileTool",
    "Event",
    "EventInvariantViolation",
    "EventStore",
    "FakeProvider",
    "FileArtifactStore",
    "HarnessError",
    "HookBus",
    "HostBackend",
    "InMemoryEventStore",
    "LocalIsolatedBackend",
    "Message",
    "ModelGateway",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "PermissionContext",
    "PermissionMode",
    "PolicyEngine",
    "Provider",
    "ReadFileTool",
    "RunCoordinator",
    "RunResult",
    "RunState",
    "RunTestsTool",
    "SQLiteEventStore",
    "SandboxBackend",
    "SandboxDescriptor",
    "SandboxViolation",
    "Session",
    "SessionNotFound",
    "ShellTool",
    "StaticApprovalResolver",
    "StopReason",
    "SuspendingApprovalResolver",
    "Telemetry",
    "TextBlock",
    "ThinkingBlock",
    "Tool",
    "ToolCallBlock",
    "ToolContext",
    "ToolInputError",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolResultBlock",
    "ToolSpec",
    "UnsafeHostNotAcknowledged",
    "Usage",
    "WriteFileTool",
    "new_id",
]
__version__ = "0.1.0"
