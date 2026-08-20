"""The two supported leaf APIs and what importing them may drag in."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import aihi.agent as agent
import aihi.models as models

# Nothing composed into a run may be advertised before it is reachable from
# RunCoordinator. `evals` is exempt: it is the read side, not a capability.
UNWIRED_PACKAGES: tuple[str, ...] = ()

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
    # analysis surface: reads a persisted log, composes into nothing
    "ReplayEngine",
    "TraceBundle",
    "TraceGraph",
    "replay_graph",
)

# Extras that the core package must never require at import time.
OPTIONAL_DISTRIBUTIONS = ("fastapi", "psycopg", "opentelemetry")


def test_public_exports_are_sorted_and_resolvable() -> None:
    for package in (models, agent):
        assert package.__all__ == sorted(package.__all__)
        assert len(package.__all__) == len(set(package.__all__))
        missing = [name for name in package.__all__ if not hasattr(package, name)]
        assert missing == []


def test_the_aihi_namespace_is_available() -> None:
    assert importlib.util.find_spec("aihi") is not None


def test_composition_root_needs_nothing_beyond_the_public_api() -> None:
    """Everything required to assemble a runtime is reachable from the top level."""

    required = {
        # the event log
        "Event",
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
        # approval boundary
        "ApprovalResolver",
        "ApprovalRequest",
        "ApprovalOutcome",
        "CompactionPolicy",
        "ContextState",
    }
    assert required <= set(agent.__all__)


def test_model_contracts_and_adapters_stay_in_the_models_leaf() -> None:
    required = {
        "Message",
        "ModelRequest",
        "ModelResponse",
        "ModelToolDefinition",
        "Provider",
        "FakeProvider",
        "OpenAIProvider",
        "AnthropicProvider",
        "DeepSeekProvider",
        "encode_message",
        "decode_message",
    }

    assert required <= set(models.__all__)
    assert required.isdisjoint(agent.__all__)
    assert {"Gateway", "ModelGateway", "ModelRouter", "ModelRoles"}.isdisjoint(
        models.__all__
    )
    assert "ToolSpec" in agent.__all__
    assert "ToolSpec" not in models.__all__


def test_tool_spec_is_owned_by_tools_without_changing_the_public_surface() -> None:
    from aihi.agent.tools import ToolSpec as tools_spec
    from aihi.agent.tools.spec import ToolSpec as direct_spec

    assert agent.ToolSpec is tools_spec is direct_spec
    assert direct_spec.__module__ == "aihi.agent.tools.spec"
    assert importlib.util.find_spec("aihi.agent.tool_spec") is None


def test_unwired_packages_are_not_advertised_as_public() -> None:
    exported = set(agent.__all__)
    for package in UNWIRED_PACKAGES:
        assert package not in exported


def test_wired_capabilities_expose_their_composition_adapters() -> None:
    """A capability reachable from RunCoordinator must be composable publicly."""

    assert set(WIRED_ADAPTERS) <= set(agent.__all__)


def test_importing_the_public_api_pulls_in_no_optional_extra() -> None:
    probe = (
        "import sys, aihi.models, aihi.agent;"
        "print(','.join(m for m in sys.modules if m.split('.')[0] in "
        f"{OPTIONAL_DISTRIBUTIONS!r}))"
    )
    source_roots = (
        Path(models.__file__).resolve().parents[2],
        Path(agent.__file__).resolve().parents[2],
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            *(str(path) for path in source_roots),
            *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert result.stdout.strip() == ""
