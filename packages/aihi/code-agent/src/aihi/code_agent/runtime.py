"""Coding Agent application assembly and the first executable agent loop."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import cast

from aihi.agent import (
    BashTool,
    DockerBackend,
    EditFileTool,
    FileSkillTrustStore,
    GlobTool,
    GrepTool,
    HostBackend,
    McpClient,
    ReadFileTool,
    Runtime,
    RuntimeBuilder,
    SandboxBackend,
    Session,
    SkillDiscovery,
    SkillLoader,
    SkillRoot,
    SkillTrustManager,
    StdioMcpTransport,
    Tool,
    WriteFileTool,
    register_mcp_tools,
)
from aihi.agent.runtime import RunResult
from aihi.models import (
    AnthropicProvider,
    DeepSeekProvider,
    FakeProvider,
    Message,
    OpenAICompatibleProvider,
    OpenAIProvider,
    Provider,
)

from .coding_tools import GitDiffTool, GitStatusTool
from .config import (
    CodeAgentConfig,
    CodeAgentConfigError,
    McpServerSettings,
    resolve_env_mapping,
)
from .skills import LoadSkillTool

_DEFAULT_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai_compatible": "AIHI_CODE_AGENT_API_KEY",
}


@dataclass(slots=True)
class CodeAgentRuntime:
    """A configured runtime plus the lifetime of its MCP child processes."""

    config: CodeAgentConfig
    runtime: Runtime
    mcp_clients: tuple[McpClient, ...] = ()

    @classmethod
    async def create(cls, config: CodeAgentConfig) -> CodeAgentRuntime:
        provider = _build_provider(config)
        sandbox = _build_sandbox(config)
        skill_loader: SkillLoader | None = None
        skill_discovery: SkillDiscovery | None = None
        if config.skill_roots:
            skill_discovery = SkillDiscovery(
                [SkillRoot(root.path, root.scope) for root in config.skill_roots]
            )
            if config.skill_trust_path is None:
                raise CodeAgentConfigError("Skill roots require a trust lockfile path")
            trust_store = FileSkillTrustStore(config.skill_trust_path)
            skill_loader = SkillLoader(
                SkillTrustManager(trust_store, discovery=skill_discovery),
                discovery=skill_discovery,
            )
        builder = RuntimeBuilder(
            provider=provider,
            model=config.provider.model,
            sandbox=sandbox,
            tools=_build_tools(config, skill_loader=skill_loader),
        )
        if skill_discovery is not None:
            builder = builder.with_skills(skill_discovery)
        runtime = builder.build()
        clients: list[McpClient] = []
        try:
            for server in config.mcp_servers:
                client = await _register_mcp_server(runtime, server)
                clients.append(client)
        except BaseException:
            for client in reversed(clients):
                await client.disconnect()
            await _close_provider(provider)
            raise
        return cls(config=config, runtime=runtime, mcp_clients=tuple(clients))

    async def run(
        self,
        session: Session,
        *,
        user_message: str,
        run_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> RunResult:
        """Run one user turn through the Harness coordinator loop."""

        text = user_message.strip()
        if not text:
            raise CodeAgentConfigError("user_message must not be empty")
        return await self.runtime.coordinator.run(
            session,
            model=model or self.config.provider.model,
            user_message=Message.text("user", text),
            run_id=run_id,
            permission_mode=self.config.permission_mode,
            require_capability_lease=self.config.require_capability_lease,
            system_prompt=(
                self.config.system_prompt if system_prompt is None else system_prompt
            ),
            max_output_tokens=max_output_tokens or self.config.max_output_tokens,
            cancel_event=cancel_event,
        )

    async def resume(
        self,
        session: Session,
        *,
        run_id: str,
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> RunResult:
        return await self.runtime.coordinator.resume(
            session,
            run_id=run_id,
            model=model,
            permission_mode=self.config.permission_mode,
            require_capability_lease=self.config.require_capability_lease,
            system_prompt=(
                self.config.system_prompt if system_prompt is None else system_prompt
            ),
            max_output_tokens=max_output_tokens,
            cancel_event=cancel_event,
        )

    async def close(self) -> None:
        for client in reversed(self.mcp_clients):
            await client.disconnect()
        await _close_provider(self.runtime.provider)


async def _register_mcp_server(runtime: Runtime, settings: McpServerSettings) -> McpClient:
    resolved_env = resolve_env_mapping(settings.env)
    transport = StdioMcpTransport(
        settings.command,
        cwd=settings.cwd,
        env=resolved_env or None,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    client = McpClient(
        transport,
        client_name="aihi-code-agent",
        reconnect_attempts=settings.reconnect_attempts,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    try:
        await register_mcp_tools(
            runtime.registry,
            client,
            server_name=settings.name,
            allowed_tools=settings.allowed_tools,
        )
    except BaseException:
        await client.disconnect()
        raise
    return client


def _build_provider(config: CodeAgentConfig) -> Provider:
    settings = config.provider
    name = settings.name.replace("-", "_").lower()
    if name == "fake":
        return FakeProvider()
    key_env = settings.api_key_env or _DEFAULT_KEY_ENV.get(name)
    if key_env is None:
        raise CodeAgentConfigError(f"Provider {settings.name!r} requires provider.api_key_env")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise CodeAgentConfigError(
            "Provider credential environment variable is missing: " + key_env
        )
    if name == "openai":
        if settings.base_url is None:
            return OpenAIProvider(api_key, timeout_seconds=settings.timeout_seconds)
        return OpenAIProvider(
            api_key, base_url=settings.base_url, timeout_seconds=settings.timeout_seconds
        )
    if name == "anthropic":
        if settings.base_url is None:
            return AnthropicProvider(api_key, timeout_seconds=settings.timeout_seconds)
        return AnthropicProvider(
            api_key, base_url=settings.base_url, timeout_seconds=settings.timeout_seconds
        )
    if name == "deepseek":
        if settings.base_url is None:
            return DeepSeekProvider(api_key, timeout_seconds=settings.timeout_seconds)
        return DeepSeekProvider(
            api_key, base_url=settings.base_url, timeout_seconds=settings.timeout_seconds
        )
    if name == "openai_compatible":
        if settings.base_url is None:
            raise CodeAgentConfigError(
                "OpenAI-compatible provider requires provider.base_url"
            )
        return OpenAICompatibleProvider(
            api_key,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        )
    raise CodeAgentConfigError(
        "Unsupported provider: " + settings.name + "; choose fake, openai, anthropic, "
        "deepseek, or openai-compatible"
    )


def _build_sandbox(config: CodeAgentConfig) -> SandboxBackend:
    settings = config.sandbox
    if settings.backend == "host":
        return HostBackend(settings.root, unsafe=settings.unsafe)
    if settings.backend == "docker":
        if settings.image is None:
            raise CodeAgentConfigError("Docker sandbox requires sandbox.image")
        return DockerBackend(
            settings.root,
            image=settings.image,
            network=settings.network,
            allow_network=settings.allow_network,
            workspace_read_only=settings.workspace_read_only,
        )
    raise CodeAgentConfigError(f"Unsupported sandbox backend: {settings.backend}")


def _build_tools(
    config: CodeAgentConfig, *, skill_loader: SkillLoader | None = None
) -> tuple[Tool, ...]:
    factories: dict[str, type[object]] = {
        "read_file": ReadFileTool,
        "glob": GlobTool,
        "grep": GrepTool,
        "edit_file": EditFileTool,
        "write_file": WriteFileTool,
        "bash": BashTool,
        "git_diff": GitDiffTool,
        "git_status": GitStatusTool,
    }
    tool_names = list(config.tools)
    if config.skill_load_tool and skill_loader is not None and "load_skill" not in tool_names:
        tool_names.append("load_skill")
    tools: list[Tool] = []
    for name in tool_names:
        if name == "load_skill":
            if skill_loader is None:
                raise CodeAgentConfigError(
                    "load_skill requires at least one configured Skill root"
                )
            tools.append(LoadSkillTool(skill_loader))
            continue
        factory = factories.get(name)
        if factory is None:
            raise CodeAgentConfigError(f"Unsupported Coding Agent tool: {name}")
        tools.append(cast(Tool, factory()))
    return tuple(tools)


async def _close_provider(provider: Provider) -> None:
    close = getattr(provider, "aclose", None)
    if close is not None:
        await close()


__all__ = ["CodeAgentRuntime"]
