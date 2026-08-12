"""The layered system prompt for the Coding Agent.

The effective prompt is the packaged coding prompt, plus what is true about
this environment, plus the workspace's own conventions, plus whatever the user
configured — in that order.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from ..config import CodeAgentConfig

_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md")
_MAX_CONVENTION_CHARS = 8_000


def load_builtin_prompt() -> str:
    """Read the packaged coding prompt."""

    resource = files(__package__ or "aihi.code_agent.prompts") / "coding.md"
    return resource.read_text(encoding="utf-8").strip()


def _environment_section(config: CodeAgentConfig, workspace: Path) -> str:
    tools = ", ".join(config.tools) or "none"
    sandbox = config.sandbox.backend
    if config.sandbox.unsafe:
        sandbox = f"{sandbox} (unsandboxed host access)"
    return (
        "## Environment\n\n"
        f"- workspace: {workspace}\n"
        f"- sandbox: {sandbox}\n"
        f"- tools: {tools}"
    )


def _conventions_section(workspace: Path) -> str:
    for name in _CONVENTION_FILES:
        candidate = workspace / name
        if not candidate.is_file():
            continue
        try:
            body = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body:
            continue
        if len(body) > _MAX_CONVENTION_CHARS:
            body = f"{body[:_MAX_CONVENTION_CHARS]}\n…(truncated)"
        return f"## Project conventions ({name})\n\n{body}"
    return ""


def compose_system_prompt(config: CodeAgentConfig, *, workspace: Path) -> str:
    """Compose the effective system prompt for a run in `workspace`."""

    if config.system_prompt_mode == "replace":
        return config.system_prompt
    sections = (
        load_builtin_prompt(),
        _environment_section(config, workspace),
        _conventions_section(workspace),
        config.system_prompt.strip(),
    )
    return "\n\n".join(section for section in sections if section)


def compose_subagent_prompt(
    config: CodeAgentConfig, *, workspace: Path, role: str
) -> str:
    """Compose one Subagent type's prompt: its role, plus the same project context.

    The top-level coding prompt is left out — the role replaces it — and so is
    `agent.system_prompt`, which instructs the main agent and in `replace` mode
    is meant to be a whole prompt of its own. Workspace conventions do carry
    over: they describe the repository, not the role.
    """

    sections = (
        role.strip(),
        _environment_section(config, workspace),
        _conventions_section(workspace),
    )
    return "\n\n".join(section for section in sections if section)


__all__ = ["compose_subagent_prompt", "compose_system_prompt", "load_builtin_prompt"]
