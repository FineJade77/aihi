"""Assemble a runtime without rewriting the wiring in every application.

The split this follows: **an application decides policy, the Harness does the
plumbing.** A choice belongs here only if every reasonable application would
make the same one *and* getting it wrong would be silent. Anything an
application should genuinely differ on is a required argument with no default —
`provider`, `sandbox` and `tools` cannot be omitted, and a subagent authority
cannot be guessed.

Nothing security-relevant is defaulted. There is deliberately no
`default_runtime()`: picking a provider or a tool set for the caller is how a
library ends up shipping product decisions.

    runtime = (
        RuntimeBuilder(provider=provider, sandbox=sandbox, tools=[ReadFileTool()])
        .with_artifacts()
        .with_telemetry(Path("telemetry.jsonl"))
        .build()
    )
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from aiharness.agents.subagent import (
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentTool,
    restrict_registry,
    subagent_session_factory,
)
from aiharness.artifacts import ArtifactStore, FileArtifactStore
from aiharness.context import ContextCompiler, ModelSummaryGenerator, SummaryGenerator
from aiharness.hooks import HookBus
from aiharness.memory import MemoryAccess, MemoryScope, MemoryService
from aiharness.memory.context import MemoryCandidateRecorder, MemoryContextContributor
from aiharness.models import ModelGateway, ModelRoles, ModelRouter, Provider
from aiharness.models.retry import RetryPolicy
from aiharness.observability import JsonlTelemetrySink, Telemetry
from aiharness.policy import ApprovalResolver, DefaultPolicyEngine, PolicyEngine
from aiharness.runtime import RunCoordinator, RuntimeExtensions
from aiharness.sandbox import SandboxBackend
from aiharness.sessions import EventStore
from aiharness.skills import SkillDiscovery, SkillIndexContributor
from aiharness.tools import Tool, ToolRegistry


@dataclass(frozen=True, slots=True)
class Runtime:
    """An assembled runtime and the parts it was built from."""

    coordinator: RunCoordinator
    provider: Provider
    registry: ToolRegistry
    sandbox: SandboxBackend
    extensions: RuntimeExtensions
    artifact_store: ArtifactStore | None = None
    telemetry: Telemetry | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBuilder:
    """Compose a runtime. Every `with_*` returns a new builder.

    Required arguments are the decisions no library should make for you.
    """

    provider: Provider
    sandbox: SandboxBackend
    tools: Sequence[Tool]
    policy: PolicyEngine | None = None
    roles: ModelRoles | None = None
    retry_policy: RetryPolicy | None = None
    approval_resolver: ApprovalResolver | None = None
    hooks: HookBus | None = None
    artifact_store: ArtifactStore | None = None
    telemetry: Telemetry | None = None
    summary_generator: SummaryGenerator | None = None
    context_contributors: tuple[object, ...] = field(default=())
    run_recorders: tuple[object, ...] = field(default=())
    context_window: int | None = None
    _subagents: _SubagentPlan | None = None

    def __post_init__(self) -> None:
        if not self.tools:
            raise ValueError(
                "A runtime needs at least one tool; which tools a model may use is an "
                "application decision the builder will not make for you."
            )

    # --- plumbing, opt in ------------------------------------------------

    def with_artifacts(self, path: str | Path | None = None) -> RuntimeBuilder:
        """Keep large tool output out of the context and out of the event log."""

        root = Path(path) if path is not None else self.sandbox.root / ".aiharness" / "artifacts"
        return replace(self, artifact_store=FileArtifactStore(root))

    def with_telemetry(self, path: str | Path) -> RuntimeBuilder:
        """Write redacted observations as JSON Lines."""

        return replace(self, telemetry=Telemetry(JsonlTelemetrySink(Path(path))))

    def with_hooks(self, hooks: HookBus) -> RuntimeBuilder:
        return replace(self, hooks=hooks)

    def with_approvals(self, resolver: ApprovalResolver) -> RuntimeBuilder:
        """Answer approval requests. Without one, a run suspends instead."""

        return replace(self, approval_resolver=resolver)

    def with_policy(self, policy: PolicyEngine) -> RuntimeBuilder:
        return replace(self, policy=policy)

    def with_model_roles(
        self, roles: ModelRoles, *, retry_policy: RetryPolicy | None = None
    ) -> RuntimeBuilder:
        return replace(self, roles=roles, retry_policy=retry_policy or self.retry_policy)

    def with_context_window(self, tokens: int) -> RuntimeBuilder:
        return replace(self, context_window=tokens)

    # --- capabilities, opt in --------------------------------------------

    def with_skills(self, discovery: SkillDiscovery) -> RuntimeBuilder:
        """Offer the skill *index*; bodies still go through the trust flow."""

        return replace(
            self,
            context_contributors=(*self.context_contributors, SkillIndexContributor(discovery)),
        )

    def with_context_contributors(self, *contributors: object) -> RuntimeBuilder:
        return replace(self, context_contributors=(*self.context_contributors, *contributors))

    def with_memory(
        self,
        service: MemoryService,
        access: MemoryAccess,
        *,
        scope: MemoryScope = MemoryScope.SESSION,
        propose: bool = True,
    ) -> RuntimeBuilder:
        """Read memory into the context, and optionally propose new candidates.

        Proposing never writes: it appends `memory.candidate` events, and
        promoting one stays an explicit `MemoryService.write`.
        """

        contributors = (
            *self.context_contributors,
            MemoryContextContributor(service, access, scope=scope),
        )
        recorders = self.run_recorders
        if propose:
            recorders = (*recorders, MemoryCandidateRecorder(service, scope=scope))
        return replace(self, context_contributors=contributors, run_recorders=recorders)

    def with_compaction(self, model: str, *, provider: Provider | None = None) -> RuntimeBuilder:
        """Use a compact model for L2 summaries, degrading to the offline one."""

        return replace(
            self,
            summary_generator=ModelSummaryGenerator(provider or self._gateway(), model),
        )

    def with_subagents(
        self,
        *,
        authority: SubagentAuthority,
        store: EventStore,
        model: str | None = None,
        provider_name: str = "",
    ) -> RuntimeBuilder:
        """Let a run delegate to a child run under `authority`.

        The authority has no default: how much of its own power a run may hand
        onward is exactly the kind of decision an application must state.
        """

        return replace(
            self,
            _subagents=_SubagentPlan(
                authority=authority,
                store=store,
                model=model,
                provider_name=provider_name or getattr(self.provider, "name", "provider"),
            ),
        )

    # --- assembly --------------------------------------------------------

    def build(self) -> Runtime:
        registry = ToolRegistry(list(self.tools))
        gateway = self._gateway()
        if self._subagents is not None:
            registry.register(self._subagent_tool(gateway, registry))
        extensions = RuntimeExtensions(
            context_contributors=self.context_contributors,  # type: ignore[arg-type]
            run_recorders=self.run_recorders,  # type: ignore[arg-type]
        )
        coordinator = RunCoordinator(
            gateway,
            registry=registry,
            sandbox=self.sandbox,
            policy=self.policy or DefaultPolicyEngine(),
            hooks=self.hooks,
            context_compiler=ContextCompiler(summary_generator=self.summary_generator),
            summary_generator=self.summary_generator,
            artifact_store=self.artifact_store,
            telemetry=self.telemetry,
            extensions=extensions,
            approval_resolver=self.approval_resolver,
            context_window=self.context_window,
        )
        return Runtime(
            coordinator=coordinator,
            provider=gateway,
            registry=registry,
            sandbox=self.sandbox,
            extensions=extensions,
            artifact_store=self.artifact_store,
            telemetry=self.telemetry,
        )

    def _gateway(self) -> Provider:
        """Route through a gateway so retries and deadlines apply to every turn.

        Wrapping is plumbing, not policy: it adds bounded retries and a request
        deadline, and only ever fails over before the first stream chunk.
        """

        if isinstance(self.provider, ModelGateway):
            return self.provider
        router = ModelRouter(default=self.provider)
        if self.roles is not None:
            for model in dict.fromkeys(self.roles.to_dict().values()):
                router.register(self.provider, models=(model,))
        return ModelGateway(
            router,
            retry_policy=self.retry_policy,
            name=getattr(self.provider, "name", "gateway"),
        )

    def _subagent_tool(self, gateway: Provider, parent: ToolRegistry) -> SubagentTool:
        plan = self._subagents
        assert plan is not None
        model = plan.model or (self.roles.resolve("subagent") if self.roles else "")
        if not model:
            raise ValueError("Subagents need a model: pass model= or with_model_roles(...)")
        sandbox = self.sandbox

        def coordinator_factory(spec: object) -> RunCoordinator:
            capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
            return RunCoordinator(
                gateway,
                registry=restrict_registry(parent, capabilities),
                sandbox=sandbox,
                policy=self.policy or DefaultPolicyEngine(),
            )

        runner = ChildRunSubagentRunner(
            coordinator_factory,
            subagent_session_factory(plan.store, provider=plan.provider_name, model=model),
            model=model,
        )
        return SubagentTool(runner, authority=plan.authority)


@dataclass(frozen=True, slots=True)
class _SubagentPlan:
    authority: SubagentAuthority
    store: EventStore
    model: str | None
    provider_name: str


__all__ = ["Runtime", "RuntimeBuilder"]
