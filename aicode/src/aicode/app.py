"""aicode composition root for the reusable AIHarness runtime."""

from __future__ import annotations

from dataclasses import dataclass

from aicode.config import AICodeConfig
from aiharness import (
    SPAWN_CAPABILITY,
    AgentBudget,
    AnthropicProvider,
    ApprovalResolver,
    ChildRunSubagentRunner,
    DefaultPolicyEngine,
    EditFileTool,
    EventStore,
    FakeProvider,
    HostBackend,
    OpenAICompatibleProvider,
    OpenAIProvider,
    Provider,
    ReadFileTool,
    RunCoordinator,
    RunTestsTool,
    RuntimeExtensions,
    SandboxBackend,
    ShellTool,
    SkillDiscovery,
    SkillIndexContributor,
    SkillRoot,
    SkillScope,
    SubagentAuthority,
    SubagentTool,
    ToolRegistry,
    WorkspaceScope,
    WriteFileTool,
    restrict_registry,
    subagent_session_factory,
)


@dataclass(frozen=True, slots=True)
class AICodeRuntime:
    """The assembled Coding Agent runtime and its reusable Harness parts."""

    coordinator: RunCoordinator
    provider: Provider
    registry: ToolRegistry
    sandbox: SandboxBackend
    extensions: RuntimeExtensions


def build_provider(config: AICodeConfig) -> Provider:
    if config.provider == "fake":
        return FakeProvider()
    if config.api_key is None:
        raise ValueError(f"AICODE_API_KEY is required for provider {config.provider}")
    if config.provider == "openai":
        return OpenAIProvider(config.api_key, **_base_url(config))
    if config.provider == "anthropic":
        return AnthropicProvider(config.api_key, **_base_url(config))
    return OpenAICompatibleProvider(config.api_key, **_base_url(config))


def build_tool_registry() -> ToolRegistry:
    """Select existing Harness tools for the Coding Agent product."""

    return ToolRegistry(
        [
            EditFileTool(),
            ReadFileTool(),
            RunTestsTool(),
            ShellTool(),
            WriteFileTool(),
        ]
    )


def build_subagent_tool(
    config: AICodeConfig, store: EventStore, sandbox: SandboxBackend
) -> SubagentTool:
    """Let a run delegate read-only investigation to a scoped child run.

    The child inherits at most this authority: a read-only workspace, no process
    execution, and no right to fan out further. Everything it does still goes
    through the same policy, hook and sandbox chain in its own session.
    """

    authority = SubagentAuthority(
        budget=AgentBudget(
            max_tokens=config.subagent_max_tokens,
            timeout_seconds=config.subagent_timeout_seconds,
            max_tool_calls=config.subagent_max_tool_calls,
        ),
        workspace=WorkspaceScope(root=str(config.workspace), read_only=True),
        capabilities=frozenset({SPAWN_CAPABILITY, "filesystem.read"}),
        max_depth=1,
        max_children=config.subagent_max_children,
    )
    parent_tools = build_tool_registry()

    def coordinator_factory(spec: object) -> RunCoordinator:
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
        return RunCoordinator(
            build_provider(config),
            registry=restrict_registry(parent_tools, capabilities),
            sandbox=sandbox,
            policy=DefaultPolicyEngine(),
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        subagent_session_factory(store, provider=config.provider, model=config.model),
        model=config.model,
    )
    return SubagentTool(runner, authority=authority)


def build_extensions(config: AICodeConfig) -> RuntimeExtensions:
    """Offer the project skill index when the workspace ships one.

    Only the index is composed here; loading a body still goes through the
    Harness trust flow. Memory needs a durable store and a scope policy, so it
    stays an explicit application choice rather than a default.
    """

    root = config.skills_path or (config.workspace / ".aicode" / "skills")
    if not root.is_dir():
        return RuntimeExtensions()
    discovery = SkillDiscovery([SkillRoot(path=root, scope=SkillScope.PROJECT)])
    return RuntimeExtensions(context_contributors=(SkillIndexContributor(discovery),))


def build_runtime(
    config: AICodeConfig,
    *,
    approval_resolver: ApprovalResolver | None = None,
    store: EventStore | None = None,
) -> AICodeRuntime:
    """Assemble aicode from existing Harness implementations.

    Without a resolver the Harness default applies: a run that needs approval
    suspends instead of guessing the answer. Subagents need somewhere to put the
    child session, so they are only registered when a store is supplied.
    """

    provider = build_provider(config)
    sandbox = HostBackend(config.workspace, unsafe=config.unsafe_host)
    registry = build_tool_registry()
    if store is not None and config.subagents:
        registry.register(build_subagent_tool(config, store, sandbox))
    extensions = build_extensions(config)
    coordinator = RunCoordinator(
        provider,
        registry=registry,
        sandbox=sandbox,
        policy=DefaultPolicyEngine(),
        approval_resolver=approval_resolver,
        extensions=extensions,
    )
    return AICodeRuntime(
        coordinator=coordinator,
        provider=provider,
        registry=registry,
        sandbox=sandbox,
        extensions=extensions,
    )


def _base_url(config: AICodeConfig) -> dict[str, str]:
    return {} if config.base_url is None else {"base_url": config.base_url}
