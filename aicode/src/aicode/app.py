"""aicode composition root for the reusable AIHarness runtime."""

from __future__ import annotations

from dataclasses import dataclass

from aicode.config import AICodeConfig
from aicode.context import ProjectRulesContributor
from aicode.prompt import SYSTEM_PROMPT
from aiharness import (
    ROLE_SUBAGENT,
    SPAWN_CAPABILITY,
    AgentBudget,
    AnthropicProvider,
    ApprovalResolver,
    ChildRunSubagentRunner,
    DefaultPolicyEngine,
    EditFileTool,
    EventStore,
    FakeProvider,
    FileArtifactStore,
    HostBackend,
    JsonlTelemetrySink,
    ModelGateway,
    ModelRouter,
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
    Telemetry,
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
    system_prompt: str
    telemetry: Telemetry | None = None


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


def build_gateway(config: AICodeConfig) -> ModelGateway:
    """Route every model request through the gateway.

    Even with a single provider this is worth it: the gateway adds bounded
    retries and a request deadline, and it only ever fails over before the
    first stream chunk, so a partially streamed turn is never replayed.
    """

    provider = build_provider(config)
    router = ModelRouter(default=provider)
    for model in dict.fromkeys(config.roles.to_dict().values()):
        router.register(provider, models=(model,))
    return ModelGateway(router, name=provider.name)


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

    subagent_model = config.roles.resolve(ROLE_SUBAGENT)

    def coordinator_factory(spec: object) -> RunCoordinator:
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
        return RunCoordinator(
            build_gateway(config),
            registry=restrict_registry(parent_tools, capabilities),
            sandbox=sandbox,
            policy=DefaultPolicyEngine(),
        )

    runner = ChildRunSubagentRunner(
        coordinator_factory,
        subagent_session_factory(store, provider=config.provider, model=subagent_model),
        model=subagent_model,
    )
    return SubagentTool(runner, authority=authority)


def build_extensions(config: AICodeConfig) -> RuntimeExtensions:
    """Compose the project's own context: its rules file and its skill index.

    Only the skill index is offered; loading a body still goes through the
    Harness trust flow. Memory needs a durable store and a scope policy, so it
    stays an explicit application choice rather than a default.
    """

    contributors: list[object] = []
    if config.project_rules:
        contributors.append(ProjectRulesContributor(config.workspace))
    root = config.skills_path or (config.workspace / ".aicode" / "skills")
    if root.is_dir():
        discovery = SkillDiscovery([SkillRoot(path=root, scope=SkillScope.PROJECT)])
        contributors.append(SkillIndexContributor(discovery))
    return RuntimeExtensions(context_contributors=tuple(contributors))  # type: ignore[arg-type]


def build_artifact_store(config: AICodeConfig) -> FileArtifactStore:
    """Keep large tool output out of the context and out of the event log."""

    root = config.artifacts_path or (config.workspace / ".aiharness" / "artifacts")
    return FileArtifactStore(root)


def build_telemetry(config: AICodeConfig) -> Telemetry | None:
    """Write redacted observations as JSON Lines when a path is configured."""

    if config.telemetry_path is None:
        return None
    return Telemetry(JsonlTelemetrySink(config.telemetry_path))


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

    provider = build_gateway(config)
    sandbox = HostBackend(config.workspace, unsafe=config.unsafe_host)
    registry = build_tool_registry()
    if store is not None and config.subagents:
        registry.register(build_subagent_tool(config, store, sandbox))
    extensions = build_extensions(config)
    telemetry = build_telemetry(config)
    coordinator = RunCoordinator(
        provider,
        registry=registry,
        sandbox=sandbox,
        policy=DefaultPolicyEngine(),
        approval_resolver=approval_resolver,
        extensions=extensions,
        artifact_store=build_artifact_store(config),
        telemetry=telemetry,
    )
    return AICodeRuntime(
        coordinator=coordinator,
        provider=provider,
        registry=registry,
        sandbox=sandbox,
        extensions=extensions,
        system_prompt=SYSTEM_PROMPT,
        telemetry=telemetry,
    )


def _base_url(config: AICodeConfig) -> dict[str, str]:
    return {} if config.base_url is None else {"base_url": config.base_url}
