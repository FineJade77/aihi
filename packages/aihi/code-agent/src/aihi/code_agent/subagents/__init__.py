"""The Coding Agent subagent roster.

Types are an application concern; the ceilings they run under stay in
`aihi.agent`. A definition can only narrow the parent's authority.
"""

from __future__ import annotations

from .authority import coding_child_context_factory, narrow_coding_child_context
from .registry import SubagentDefinition

CODING_SUBAGENTS: tuple[SubagentDefinition, ...] = (
    SubagentDefinition(
        name="explore",
        description="Read-only search across the repository. Returns findings, not edits.",
        prompt_file="explore.md",
        capabilities=frozenset({"filesystem.read"}),
        tools=("read_file", "glob", "grep"),
    ),
    SubagentDefinition(
        name="code_review",
        description="Read-only review of the current diff for correctness defects.",
        prompt_file="code_review.md",
        capabilities=frozenset({"filesystem.read"}),
        tools=("read_file", "glob", "grep", "git_status", "git_diff"),
    ),
    SubagentDefinition(
        name="test",
        description="Write and run tests. Needs process execution, so it is off by default.",
        prompt_file="test.md",
        capabilities=frozenset({"filesystem.read", "filesystem.write", "process.exec"}),
        tools=("read_file", "glob", "grep", "edit_file", "write_file", "bash"),
    ),
    SubagentDefinition(
        name="general",
        description="A scoped objective inheriting this run's capabilities.",
        prompt_file="general.md",
    ),
)

SUBAGENT_TYPE_NAMES = frozenset(definition.name for definition in CODING_SUBAGENTS)


def definition_for(name: str) -> SubagentDefinition:
    """Return the definition named `name`, or raise `KeyError`."""

    for definition in CODING_SUBAGENTS:
        if definition.name == name:
            return definition
    raise KeyError(f"Unknown subagent type: {name}")


__all__ = [
    "CODING_SUBAGENTS",
    "SUBAGENT_TYPE_NAMES",
    "SubagentDefinition",
    "coding_child_context_factory",
    "definition_for",
    "narrow_coding_child_context",
]
