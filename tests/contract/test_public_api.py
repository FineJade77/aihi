"""The public composition surface and what importing it may drag in."""

import os
import subprocess
import sys
from pathlib import Path

import aiharness

# Packages that exist but are not yet injectable into RunCoordinator. Promoting
# one of these is a composition-contract change and needs an ADR (TASK.md H-02).
UNWIRED_PACKAGES = ("evals", "api", "cli")

# Capabilities that RunCoordinator can compose, so their adapters are public.
WIRED_ADAPTERS = (
    "RuntimeExtensions",
    "ContextContributor",
    "ContextRequest",
    "ContextSection",
    "RunRecorder",
    "RunOutcome",
    "SkillIndexContributor",
    "SkillDiscovery",
    "MemoryContextContributor",
    "MemoryCandidateRecorder",
    "MemoryService",
    "MemoryAccess",
    "SubagentTool",
    "SubagentAuthority",
    "ChildRunSubagentRunner",
    "AgentBudget",
    "WorkspaceScope",
    "register_mcp_tools",
    "register_plugin_tools",
    "StdioMcpTransport",
    "PluginHostPolicy",
)

# Extras that the core package must never require at import time.
OPTIONAL_DISTRIBUTIONS = ("fastapi", "psycopg", "opentelemetry")


def test_public_exports_are_sorted_and_resolvable() -> None:
    assert aiharness.__all__ == sorted(aiharness.__all__)
    assert len(aiharness.__all__) == len(set(aiharness.__all__))
    missing = [name for name in aiharness.__all__ if not hasattr(aiharness, name)]
    assert missing == []


def test_composition_root_needs_nothing_beyond_the_public_api() -> None:
    """Everything required to assemble a runtime is reachable from the top level."""

    required = {
        # canonical types and the event log
        "Event",
        "Message",
        "Session",
        "EventStore",
        "SQLiteEventStore",
        # the run loop
        "RunCoordinator",
        "RunResult",
        "RunState",
        # the four injection points of the side-effect chain
        "ToolRegistry",
        "PolicyEngine",
        "HookBus",
        "SandboxBackend",
        # provider adapters
        "Provider",
        "FakeProvider",
        # approval boundary
        "ApprovalResolver",
        "ApprovalRequest",
        "ApprovalOutcome",
    }
    assert required <= set(aiharness.__all__)


def test_unwired_packages_are_not_advertised_as_public() -> None:
    exported = set(aiharness.__all__)
    for package in UNWIRED_PACKAGES:
        assert package not in exported


def test_wired_capabilities_expose_their_composition_adapters() -> None:
    """A capability reachable from RunCoordinator must be composable publicly."""

    assert set(WIRED_ADAPTERS) <= set(aiharness.__all__)


def test_importing_the_public_api_pulls_in_no_optional_extra() -> None:
    probe = (
        "import sys, aiharness;"
        "print(','.join(m for m in sys.modules if m.split('.')[0] in "
        f"{OPTIONAL_DISTRIBUTIONS!r}))"
    )
    source_root = Path(aiharness.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert result.stdout.strip() == ""
