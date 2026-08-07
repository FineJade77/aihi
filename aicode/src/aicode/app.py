"""aicode composition root for the reusable AIHarness runtime."""

from __future__ import annotations

from dataclasses import dataclass

from aicode.config import AICodeConfig
from aiharness import (
    AnthropicProvider,
    ApprovalResolver,
    DefaultPolicyEngine,
    EditFileTool,
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
    ToolRegistry,
    WriteFileTool,
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
    config: AICodeConfig, *, approval_resolver: ApprovalResolver | None = None
) -> AICodeRuntime:
    """Assemble aicode from existing Harness implementations.

    Without a resolver the Harness default applies: a run that needs approval
    suspends instead of guessing the answer.
    """

    provider = build_provider(config)
    sandbox = HostBackend(config.workspace, unsafe=config.unsafe_host)
    registry = build_tool_registry()
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
