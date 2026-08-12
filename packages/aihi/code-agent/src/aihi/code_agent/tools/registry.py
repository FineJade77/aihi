"""Declarative definition of the Coding Agent tool set."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aihi.agent import ReadLedger, SkillLoader, Tool

from ..config import CodeAgentConfig


@dataclass(frozen=True, slots=True)
class ToolBuildContext:
    """Everything a tool factory may need in order to construct its tool."""

    config: CodeAgentConfig
    skill_loader: SkillLoader | None = None
    # Shared by read_file and the mutating tools so an edit cannot precede a read.
    ledger: ReadLedger = field(default_factory=ReadLedger)

    def has(self, requirement: str) -> bool:
        return getattr(self, requirement, None) is not None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One tool the Coding Agent can offer, and how to construct it.

    Adding a tool means adding a definition, not editing the runtime assembly.
    """

    name: str
    factory: Callable[[ToolBuildContext], Tool]
    default_enabled: bool = True
    requires: tuple[str, ...] = ()

    def available(self, context: ToolBuildContext) -> bool:
        """Whether every dependency this tool needs is present."""

        return all(context.has(requirement) for requirement in self.requires)


__all__ = ["ToolBuildContext", "ToolDefinition"]
