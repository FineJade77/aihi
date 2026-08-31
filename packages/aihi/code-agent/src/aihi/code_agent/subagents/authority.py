"""Coding-specific child authority narrowing.

The Harness carries this result opaquely. Only the Coding application knows
that filesystem and process capabilities map to AccessMode, or that a plan Run
must keep every child read-only in the same Session workspace.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aihi.agent import (
    ChildRunContext,
    EventStore,
    Session,
    SessionFactory,
    TaskSpec,
    ToolContext,
)

from ..permissions import (
    AccessMode,
    CodeAgentPermissionContext,
    RunMode,
    build_run_profile,
)

_ACCESS_RANK = {
    AccessMode.READ_ONLY: 0,
    AccessMode.WORKSPACE_WRITE: 1,
    AccessMode.FULL_ACCESS: 2,
}


def narrow_coding_child_context(
    parent: CodeAgentPermissionContext,
    capabilities: frozenset[str],
) -> CodeAgentPermissionContext:
    """Intersect requested child capabilities with the parent's authority."""

    if parent.run_mode is RunMode.PLAN:
        access_mode = AccessMode.READ_ONLY
    else:
        requested = _access_for_capabilities(capabilities)
        access_mode = min(
            (parent.access_mode, requested),
            key=_ACCESS_RANK.__getitem__,
        )
    return CodeAgentPermissionContext(
        workspace=parent.workspace,
        access_mode=access_mode,
        run_mode=parent.run_mode,
        command_sandbox=parent.command_sandbox,
    )


def coding_child_context_factory() -> Callable[[TaskSpec, ToolContext[object]], ChildRunContext]:
    """Build the application callback required by generic delegation."""

    def factory(spec: TaskSpec, context: ToolContext[object]) -> ChildRunContext:
        parent = context.app_context
        if not isinstance(parent, CodeAgentPermissionContext):
            raise ValueError("Coding subagents require a parent application context")
        child = narrow_coding_child_context(parent, spec.capabilities)
        return ChildRunContext(
            app_context=child,
            run_profile=build_run_profile(child),
        )

    return factory


def coding_session_factory(
    store: EventStore,
    *,
    workspace: Path,
    provider: str,
    model: str,
) -> SessionFactory:
    """Create child Sessions in the parent Coding workspace."""

    def factory(spec: TaskSpec, context: ToolContext[object]) -> Session:
        return Session.create(
            store,
            cwd=workspace,
            provider=provider,
            model=model,
            metadata={
                "parent_session_id": context.session_id,
                "parent_run_id": context.run_id,
                "task_id": spec.task_id,
                "depth": spec.depth,
            },
        )

    return factory


def _access_for_capabilities(capabilities: frozenset[str]) -> AccessMode:
    if "process.exec" in capabilities:
        return AccessMode.FULL_ACCESS
    if "filesystem.write" in capabilities:
        return AccessMode.WORKSPACE_WRITE
    return AccessMode.READ_ONLY


__all__ = [
    "coding_child_context_factory",
    "coding_session_factory",
    "narrow_coding_child_context",
]
