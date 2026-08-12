"""The Coding Agent tool set: one place that says which tools exist."""

from __future__ import annotations

from aihi.agent import (
    BashTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    Tool,
    WriteFileTool,
)

from ..config import CodeAgentConfigError
from .git import GitDiffTool, GitStatusTool
from .registry import ToolBuildContext, ToolDefinition
from .skill import LoadSkillTool


def _load_skill(context: ToolBuildContext) -> Tool:
    assert context.skill_loader is not None  # guarded by ToolDefinition.requires
    return LoadSkillTool(context.skill_loader)


CODING_TOOLSET: tuple[ToolDefinition, ...] = (
    ToolDefinition("read_file", lambda ctx: ReadFileTool(ledger=ctx.ledger)),
    ToolDefinition("glob", lambda _: GlobTool()),
    ToolDefinition("grep", lambda _: GrepTool()),
    ToolDefinition("git_status", lambda _: GitStatusTool()),
    ToolDefinition("git_diff", lambda _: GitDiffTool()),
    ToolDefinition("edit_file", lambda ctx: EditFileTool(ledger=ctx.ledger)),
    ToolDefinition("write_file", lambda ctx: WriteFileTool(ledger=ctx.ledger)),
    ToolDefinition("bash", lambda _: BashTool()),
    ToolDefinition("load_skill", _load_skill, requires=("skill_loader",)),
)


def build_tools(context: ToolBuildContext) -> tuple[Tool, ...]:
    """Build the configured tools in configured order.

    An unknown name is an error rather than a silent omission: a typo in the
    tool allowlist should not quietly hand the agent a smaller tool set.
    """

    definitions = {definition.name: definition for definition in CODING_TOOLSET}
    names = list(context.config.tools)
    if (
        context.config.skill_load_tool
        and context.skill_loader is not None
        and "load_skill" not in names
    ):
        names.append("load_skill")
    tools: list[Tool] = []
    for name in names:
        definition = definitions.get(name)
        if definition is None:
            raise CodeAgentConfigError(f"Unsupported Coding Agent tool: {name}")
        if not definition.available(context):
            raise CodeAgentConfigError(
                f"Tool {name} requires at least one configured Skill root"
                if "skill_loader" in definition.requires
                else f"Tool {name} is missing a dependency: {', '.join(definition.requires)}"
            )
        tools.append(definition.factory(context))
    return tuple(tools)


__all__ = [
    "CODING_TOOLSET",
    "GitDiffTool",
    "GitStatusTool",
    "LoadSkillTool",
    "ToolBuildContext",
    "ToolDefinition",
    "build_tools",
]
