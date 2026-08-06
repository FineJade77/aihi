"""aicode composition root for the reusable AIHarness runtime."""

from __future__ import annotations

from dataclasses import dataclass

from aicode.config import AICodeConfig
from aiharness.models.base import Provider
from aiharness.models.providers import AnthropicProvider, OpenAICompatibleProvider, OpenAIProvider
from aiharness.models.providers.fake import FakeProvider
from aiharness.policy import ApprovalResolver, DefaultPolicyEngine
from aiharness.runtime import RunCoordinator
from aiharness.sandbox.base import SandboxBackend
from aiharness.sandbox.host import HostBackend
from aiharness.tools import ToolRegistry
from aiharness.tools.builtin import (
    EditFileTool,
    ReadFileTool,
    RunTestsTool,
    ShellTool,
    WriteFileTool,
)


@dataclass(frozen=True, slots=True)
class AICodeRuntime:
    """The assembled Coding Agent runtime and its reusable Harness parts."""

    coordinator: RunCoordinator
    provider: Provider
    registry: ToolRegistry
    sandbox: SandboxBackend


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
    coordinator = RunCoordinator(
        provider,
        registry=registry,
        sandbox=sandbox,
        policy=DefaultPolicyEngine(),
        approval_resolver=approval_resolver,
    )
    return AICodeRuntime(
        coordinator=coordinator,
        provider=provider,
        registry=registry,
        sandbox=sandbox,
    )


def _base_url(config: AICodeConfig) -> dict[str, str]:
    return {} if config.base_url is None else {"base_url": config.base_url}
