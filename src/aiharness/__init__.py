"""AIHarness public composition API.

This module is the whole supported surface for applications (`aicode/`,
`personal/`, …). Everything an application needs to assemble a runtime is
re-exported here; anything reachable only through a submodule path is internal
and may change without an ADR.

Deliberately absent: `plugins`, `mcp`, `evals`, `api` and `cli`. Those
packages exist but are not yet injectable into `RunCoordinator`, so there is no
composition contract to promise (TASK.md H-02). They stay on explicit submodule
imports until the Runtime wires them; promoting one means adding its injection
point and an ADR, in that order.

Importing this module must not require any optional extra (`fastapi`,
`psycopg`, `opentelemetry`); `tests/contract/test_public_api.py` enforces that.
"""

from aiharness.agents import (
    SPAWN_CAPABILITY,
    AgentBudget,
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentRunner,
    SubagentTool,
    WorkspaceScope,
    restrict_registry,
    subagent_session_factory,
)
from aiharness.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactStore, FileArtifactStore
from aiharness.context import ContextCompiler, ContextSection
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
from aiharness.memory import (
    InMemoryMemoryStore,
    MemoryAccess,
    MemoryCandidate,
    MemoryCandidateRecorder,
    MemoryContextContributor,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryService,
    MemoryStore,
)
from aiharness.models import ModelGateway, ModelRouter, Provider
from aiharness.models.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from aiharness.models.providers.fake import FakeProvider
from aiharness.observability import (
    InMemoryTelemetrySink,
    JsonlTelemetrySink,
    Telemetry,
    TelemetrySink,
)
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
from aiharness.runtime import (
    ContextContributor,
    ContextRequest,
    RunCoordinator,
    RunOutcome,
    RunRecorder,
    RunResult,
    RunState,
    RuntimeExtensions,
)
from aiharness.sandbox import (
    DockerBackend,
    HostBackend,
    LocalIsolatedBackend,
    SandboxBackend,
    SandboxDescriptor,
)
from aiharness.sessions import EventStore, InMemoryEventStore, Session, SQLiteEventStore
from aiharness.skills import SkillDiscovery, SkillIndexContributor, SkillRoot, SkillScope
from aiharness.tools import Tool, ToolContext, ToolRegistry, ToolResult
from aiharness.tools.builtin import (
    EditFileTool,
    ReadFileTool,
    RunTestsTool,
    ShellTool,
    WriteFileTool,
)

__all__ = [
    "AgentBudget",
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
    "ChildRunSubagentRunner",
    "ContentBlock",
    "ContextCompiler",
    "ContextContributor",
    "ContextRequest",
    "ContextSection",
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
    "InMemoryMemoryStore",
    "InMemoryTelemetrySink",
    "JsonlTelemetrySink",
    "LocalIsolatedBackend",
    "MemoryAccess",
    "MemoryCandidate",
    "MemoryCandidateRecorder",
    "MemoryContextContributor",
    "MemoryKind",
    "MemoryRecord",
    "MemoryScope",
    "MemoryService",
    "MemoryStore",
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
    "RunOutcome",
    "RunRecorder",
    "RunResult",
    "RunState",
    "RunTestsTool",
    "RuntimeExtensions",
    "SPAWN_CAPABILITY",
    "SQLiteEventStore",
    "SandboxBackend",
    "SandboxDescriptor",
    "SandboxViolation",
    "Session",
    "SessionNotFound",
    "ShellTool",
    "SkillDiscovery",
    "SkillIndexContributor",
    "SkillRoot",
    "SkillScope",
    "StaticApprovalResolver",
    "StopReason",
    "SubagentAuthority",
    "SubagentRunner",
    "SubagentTool",
    "SuspendingApprovalResolver",
    "Telemetry",
    "TelemetrySink",
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
    "WorkspaceScope",
    "WriteFileTool",
    "new_id",
    "restrict_registry",
    "subagent_session_factory",
]
__version__ = "0.1.0"
