"""aicode composition root for the reusable AIHarness runtime."""

from __future__ import annotations

from dataclasses import dataclass

from aicode.config import AICodeConfig
from aicode.context import ProjectRulesContributor
from aicode.hooks import FormatOnEditHook, register_format_hook
from aicode.prompt import SYSTEM_PROMPT
from aiharness import (
    ROLE_COMPACT,
    ROLE_SUBAGENT,
    SPAWN_CAPABILITY,
    AgentBudget,
    AnthropicProvider,
    ApprovalResolver,
    BashTool,
    EditFileTool,
    EventStore,
    FakeProvider,
    GlobTool,
    GrepTool,
    HookBus,
    HostBackend,
    OpenAICompatibleProvider,
    OpenAIProvider,
    Provider,
    ReadFileTool,
    RunCoordinator,
    RuntimeBuilder,
    RuntimeExtensions,
    SandboxBackend,
    SkillDiscovery,
    SkillRoot,
    SkillScope,
    SubagentAuthority,
    Telemetry,
    Tool,
    ToolRegistry,
    WorkspaceScope,
    WriteFileTool,
    resolve_bash,
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
    if config.base_url is None:
        if config.provider == "openai":
            return OpenAIProvider(config.api_key)
        if config.provider == "anthropic":
            return AnthropicProvider(config.api_key)
        return OpenAICompatibleProvider(config.api_key)
    if config.provider == "openai":
        return OpenAIProvider(config.api_key, base_url=config.base_url)
    if config.provider == "anthropic":
        return AnthropicProvider(config.api_key, base_url=config.base_url)
    return OpenAICompatibleProvider(config.api_key, base_url=config.base_url)


def build_tool_registry() -> list[Tool]:
    """Which tools this product gives the model — a product decision."""

    return [BashTool(), EditFileTool(), GlobTool(), GrepTool(), ReadFileTool(), WriteFileTool()]


def build_subagent_authority(config: AICodeConfig) -> SubagentAuthority:
    """How much of its own power a run may delegate — also a product decision."""

    return SubagentAuthority(
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


def build_context_contributors(config: AICodeConfig) -> list[object]:
    """The project's own context: its rules file and its skill index."""

    contributors: list[object] = []
    if config.project_rules:
        contributors.append(ProjectRulesContributor(config.workspace))
    return contributors


def build_hooks(config: AICodeConfig, sandbox: SandboxBackend) -> HookBus:
    """Register the project's lifecycle hooks.

    Configuring `AICODE_FORMAT_COMMAND` is the act of trust the Harness requires
    for a mutating hook; it still only runs when the enclosing tool call was
    allowed and the sandbox is acknowledged.
    """

    bus = HookBus()
    if config.format_command is not None:
        register_format_hook(
            bus,
            FormatOnEditHook(config.format_command, sandbox, shell_path=resolve_bash()),
        )
    return bus


def build_runtime(
    config: AICodeConfig,
    *,
    approval_resolver: ApprovalResolver | None = None,
    store: EventStore | None = None,
) -> AICodeRuntime:
    """Assemble aicode from Harness parts.

    Only the product decisions live here: which provider, which tools, the
    prompt, and how much authority a subagent may inherit. The wiring is the
    builder's job.
    """

    sandbox = HostBackend(config.workspace, unsafe=config.unsafe_host)
    builder = (
        RuntimeBuilder(
            provider=build_provider(config),
            sandbox=sandbox,
            tools=build_tool_registry(),
        )
        .with_model_roles(config.roles)
        .with_hooks(build_hooks(config, sandbox))
        .with_artifacts(config.artifacts_path)
        .with_context_contributors(*build_context_contributors(config))
    )
    if approval_resolver is not None:
        builder = builder.with_approvals(approval_resolver)
    if config.telemetry_path is not None:
        builder = builder.with_telemetry(config.telemetry_path)
    if config.compact_model is not None:
        builder = builder.with_compaction(config.roles.resolve(ROLE_COMPACT))
    skills_root = config.skills_path or (config.workspace / ".aicode" / "skills")
    if skills_root.is_dir():
        builder = builder.with_skills(
            SkillDiscovery([SkillRoot(path=skills_root, scope=SkillScope.PROJECT)])
        )
    if store is not None and config.subagents:
        builder = builder.with_subagents(
            authority=build_subagent_authority(config),
            store=store,
            model=config.roles.resolve(ROLE_SUBAGENT),
            provider_name=config.provider,
        )
    runtime = builder.build()
    return AICodeRuntime(
        coordinator=runtime.coordinator,
        provider=runtime.provider,
        registry=runtime.registry,
        sandbox=runtime.sandbox,
        extensions=runtime.extensions,
        system_prompt=SYSTEM_PROMPT,
        telemetry=runtime.telemetry,
    )
