"""Assemble a runtime without rewriting the wiring in every application.

The split this follows: **an application decides policy, the Harness does the
plumbing.** A choice belongs here only if every reasonable application would
make the same one *and* getting it wrong would be silent. Anything an
application should genuinely differ on is a required argument with no default —
`provider`, `model`, `sandbox` and `tools` cannot be omitted, and a subagent authority
cannot be guessed.

Nothing security-relevant is defaulted. There is deliberately no
`default_runtime()`: picking a provider or a tool set for the caller is how a
library ends up shipping product decisions.

    runtime = (
        RuntimeBuilder(
            provider=provider,
            model="model-id",
            sandbox=sandbox,
            tools=[ReadFileTool()],
        )
        .with_artifacts()
        .with_telemetry(Path("telemetry.jsonl"))
        .build()
    )
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from aihi.agent.agents.subagent import (
    ChildRunSubagentRunner,
    SubagentAuthority,
    SubagentRunner,
    SubagentTool,
    SubagentTypeSpec,
    restrict_registry,
    subagent_session_factory,
)
from aihi.agent.artifacts import ArtifactStore, FileArtifactStore
from aihi.agent.context import ContextCompiler, ModelSummaryGenerator, SummaryGenerator
from aihi.agent.hooks import HookBus
from aihi.agent.memory import MemoryAccess, MemoryScope, MemoryService
from aihi.agent.memory.context import MemoryCandidateRecorder, MemoryContextContributor
from aihi.agent.observability import JsonlTelemetrySink, Telemetry
from aihi.agent.policy import ApprovalResolver, DefaultPolicyEngine, PolicyEngine
from aihi.agent.runtime import RunCoordinator, RuntimeExtensions
from aihi.agent.sandbox import SandboxBackend
from aihi.agent.sessions import EventStore
from aihi.agent.skills import SkillDiscovery, SkillIndexContributor
from aihi.agent.tools import Tool, ToolRegistry
from aihi.models import Provider


@dataclass(frozen=True, slots=True)
class Runtime:
    """An assembled runtime and the parts it was built from."""

    coordinator: RunCoordinator
    provider: Provider
    model: str
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
    model: str
    sandbox: SandboxBackend
    tools: Sequence[Tool]
    policy: PolicyEngine | None = None
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
        if not self.model.strip():
            raise ValueError("A runtime model must be a non-empty string")
        if not self.tools:
            raise ValueError(
                "A runtime needs at least one tool; which tools a model may use is an "
                "application decision the builder will not make for you."
            )

    # --- plumbing, opt in ------------------------------------------------

    def with_artifacts(self, path: str | Path | None = None) -> RuntimeBuilder:
        """Keep large tool output out of the context and out of the event log."""

        root = Path(path) if path is not None else self.sandbox.root / ".aihi" / "artifacts"
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

    def with_compaction(self, *, provider: Provider, model: str) -> RuntimeBuilder:
        """Use a compact model for L2 summaries, degrading to the offline one."""

        return replace(
            self,
            summary_generator=ModelSummaryGenerator(provider, model),
        )

    def with_subagents(
        self,
        *,
        authority: SubagentAuthority,
        store: EventStore,
        provider: Provider,
        model: str,
        runners: Mapping[str, SubagentRunner] | None = None,
        agent_types: Mapping[str, SubagentTypeSpec] | None = None,
    ) -> RuntimeBuilder:
        """Let a run delegate to a child run under `authority`.

        The authority has no default: how much of its own power a run may hand
        onward is exactly the kind of decision an application must state.
        """

        return replace(
            self,
            _subagents=_SubagentPlan(
                runners=runners,
                agent_types=agent_types,
                authority=authority,
                store=store,
                provider=provider,
                model=model,
            ),
        )

    # --- assembly --------------------------------------------------------

    def build(self) -> Runtime:
        registry = ToolRegistry(list(self.tools))
        if self._subagents is not None:
            registry.register(self._subagent_tool(registry))
        extensions = RuntimeExtensions(
            context_contributors=self.context_contributors,  # type: ignore[arg-type]
            run_recorders=self.run_recorders,  # type: ignore[arg-type]
        )
        coordinator = RunCoordinator(
            self.provider,
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
            provider=self.provider,
            model=self.model,
            registry=registry,
            sandbox=self.sandbox,
            extensions=extensions,
            artifact_store=self.artifact_store,
            telemetry=self.telemetry,
        )

    def _subagent_tool(self, parent: ToolRegistry) -> SubagentTool:
        plan = self._subagents
        assert plan is not None
        sandbox = self.sandbox

        def coordinator_factory(spec: object, child_sandbox: SandboxBackend) -> RunCoordinator:
            capabilities = frozenset(getattr(spec, "capabilities", frozenset()))
            return RunCoordinator(
                plan.provider,
                registry=restrict_registry(parent, capabilities),
                sandbox=child_sandbox,
                policy=self.policy or DefaultPolicyEngine(),
            )

        def make_runner(system_prompt: str, model: str) -> ChildRunSubagentRunner:
            return ChildRunSubagentRunner(
                coordinator_factory,
                subagent_session_factory(
                    plan.store,
                    provider=getattr(plan.provider, "name", "provider"),
                    model=model,
                ),
                sandbox=sandbox,
                model=model,
                system_prompt=system_prompt,
            )

        if plan.runners:
            return SubagentTool(plan.runners, authority=plan.authority)
        if plan.agent_types:
            runners: dict[str, SubagentRunner] = {
                name: make_runner(spec.system_prompt, spec.model or plan.model)
                for name, spec in plan.agent_types.items()
            }
            runners.setdefault("general", make_runner("", plan.model))
            return SubagentTool(runners, authority=plan.authority)
        return SubagentTool(make_runner("", plan.model), authority=plan.authority)


@dataclass(frozen=True, slots=True)
class _SubagentPlan:
    authority: SubagentAuthority
    store: EventStore
    provider: Provider
    model: str
    runners: Mapping[str, SubagentRunner] | None = None
    agent_types: Mapping[str, SubagentTypeSpec] | None = None


__all__ = ["Runtime", "RuntimeBuilder"]
