"""Coding Agent application assembly and the first executable agent loop."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aihi.agent import (
    AgentBudget,
    AgentRuntimeError,
    DockerBackend,
    EventStore,
    FileSkillTrustStore,
    HostBackend,
    McpClient,
    Runtime,
    RuntimeBuilder,
    SandboxBackend,
    Session,
    SkillDiscovery,
    SkillLoader,
    SkillRoot,
    SkillTrustManager,
    StdioMcpTransport,
    SubagentAuthority,
    SubagentTypeSpec,
    WorkspaceScope,
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

from .config import (
    CodeAgentConfig,
    CodeAgentConfigError,
    McpServerSettings,
    resolve_env_mapping,
)
from .prompts import build_subagent_prompt, build_system_prompt
from .skills import builtin_skill_root
from .subagents import CODING_SUBAGENTS
from .tools import ToolBuildContext, build_tools
from .turns import TurnEvent, TurnEventPump, TurnFinished, drive_turn

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
    pump: TurnEventPump = field(default_factory=TurnEventPump)

    @classmethod
    async def create(
        cls, config: CodeAgentConfig, *, store: EventStore | None = None
    ) -> CodeAgentRuntime:
        provider = _build_provider(config)
        sandbox = _build_sandbox(config)
        configured_roots = [SkillRoot(root.path, root.scope) for root in config.skill_roots]
        # Only configured roots need a lockfile: a BUILTIN Skill's integrity is
        # the package's integrity, so demanding extra trust adds ceremony, not
        # safety — and would force every user to configure one.
        if configured_roots and config.skill_trust_path is None:
            raise CodeAgentConfigError("Skill roots require a trust lockfile path")
        skill_discovery = SkillDiscovery([builtin_skill_root(), *configured_roots])
        trust_store = FileSkillTrustStore(
            config.skill_trust_path
            if config.skill_trust_path is not None
            else config.base_dir / ".aihi" / "skills.lock.json"
        )
        skill_loader = SkillLoader(
            SkillTrustManager(trust_store, discovery=skill_discovery),
            discovery=skill_discovery,
        )
        builder = RuntimeBuilder(
            provider=provider,
            model=config.provider.model,
            sandbox=sandbox,
            tools=build_tools(ToolBuildContext(config=config, skill_loader=skill_loader)),
        )
        builder = builder.with_skills(skill_discovery)
        if config.artifact_path is not None:
            builder = builder.with_artifacts(config.artifact_path)
        if config.compact_model is not None:
            builder = builder.with_compaction(provider=provider, model=config.compact_model)
        if config.context_window is not None:
            builder = builder.with_context_window(config.context_window)
        if config.subagents.enabled:
            if store is None:
                raise CodeAgentConfigError(
                    "Enabled subagents require a Session EventStore"
                )
            authority = SubagentAuthority(
                budget=AgentBudget(
                    max_tokens=config.subagents.max_tokens,
                    timeout_seconds=config.subagents.timeout_seconds,
                    max_tool_calls=config.subagents.max_tool_calls,
                ),
                workspace=WorkspaceScope(
                    root=str(config.sandbox.root),
                    read_only=config.sandbox.workspace_read_only,
                ),
                capabilities=config.subagents.capabilities,
                max_depth=config.subagents.max_depth,
                max_children=config.subagents.max_children,
            )
            builder = builder.with_subagents(
                authority=authority,
                store=store,
                provider=provider,
                model=config.subagents.model or config.provider.model,
                agent_types=_build_agent_types(config),
            )
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

    def stream(
        self,
        session: Session,
        *,
        user_message: str,
        run_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """Stream one user turn as typed domain events, ending in `TurnFinished`."""

        text = user_message.strip()
        if not text:
            raise CodeAgentConfigError("user_message must not be empty")

        def invoke() -> Coroutine[Any, Any, RunResult]:
            return self.runtime.coordinator.run(
                session,
                model=model or self.config.provider.model,
                user_message=Message.text("user", text),
                run_id=run_id,
                permission_mode=self.config.permission_mode,
                require_capability_lease=self.config.require_capability_lease,
                system_prompt=(
                    build_system_prompt(self.config, workspace=Path(session.cwd))
                    if system_prompt is None
                    else system_prompt
                ),
                max_output_tokens=max_output_tokens or self.config.max_output_tokens,
                cancel_event=cancel_event,
            )

        return drive_turn(session=session, pump=self.pump, invoke=invoke)

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
        """Run one user turn through the Harness coordinator loop.

        Implemented on top of `stream()` so there is only one execution path.
        """

        final: RunResult | None = None
        async for event in self.stream(
            session,
            user_message=user_message,
            run_id=run_id,
            model=model,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            cancel_event=cancel_event,
        ):
            if isinstance(event, TurnFinished):
                final = event.result
        if final is None:  # pragma: no cover - drive_turn always ends with TurnFinished
            raise AgentRuntimeError("Turn ended without a result")
        return final

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
                build_system_prompt(self.config, workspace=Path(session.cwd))
                if system_prompt is None
                else system_prompt
            ),
            max_output_tokens=max_output_tokens,
            cancel_event=cancel_event,
        )

    async def close(self) -> None:
        for client in reversed(self.mcp_clients):
            await client.disconnect()
        await _close_provider(self.runtime.provider)


def _build_agent_types(config: CodeAgentConfig) -> dict[str, SubagentTypeSpec]:
    """Declare each enabled Subagent type's prompt and model to the builder."""

    declared: dict[str, SubagentTypeSpec] = {}
    for definition in CODING_SUBAGENTS:
        override = config.subagents.types.get(definition.name)
        if override is not None and not override.enabled:
            continue
        model = (override.model if override is not None else None) or definition.model
        declared[definition.name] = SubagentTypeSpec(
            system_prompt=build_subagent_prompt(
                config, workspace=config.sandbox.root, role=definition.prompt()
            ),
            model=model,
            capabilities=definition.capabilities or None,
            tools=definition.tools,
        )
    return declared


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


async def _close_provider(provider: Provider) -> None:
    close = getattr(provider, "aclose", None)
    if close is not None:
        await close()


__all__ = ["CodeAgentRuntime"]
