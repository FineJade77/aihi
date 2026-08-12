"""The layered system prompt for the Coding Agent.

The application owns this prompt outright — there is no user-configurable
override. Refining agent behaviour means editing `coding.md`; project-specific
rules belong in the workspace's own `AGENTS.md`, which is picked up here.

Named `build_*` rather than `compose_*`: `aihi.agent.context` already exports a
`compose_system_prompt(system_prompt, sections)` that joins a base prompt with
contributed sections, and two functions with one name would be a trap.
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
    """State what is true about this run's surroundings.

    The tool set is deliberately absent: the model already receives every tool
    as a native schema on `ModelRequest.tools`, and the registry a child run
    sees is narrower than the configured allowlist — listing names here would
    duplicate that channel and, for a subagent, contradict it.
    """

    sandbox = config.sandbox.backend
    if config.sandbox.unsafe:
        sandbox = f"{sandbox} (unsandboxed host access)"
    return f"## Environment\n\n- workspace: {workspace}\n- sandbox: {sandbox}"


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


def build_system_prompt(config: CodeAgentConfig, *, workspace: Path) -> str:
    """Build the effective system prompt for a run in `workspace`."""

    sections = (
        load_builtin_prompt(),
        _environment_section(config, workspace),
        _conventions_section(workspace),
    )
    return "\n\n".join(section for section in sections if section)


def build_subagent_prompt(config: CodeAgentConfig, *, workspace: Path, role: str) -> str:
    """Build one Subagent type's prompt: its role, plus the same project context.

    The top-level coding prompt is left out — the role replaces it. Workspace
    conventions do carry over: they describe the repository, not the role.
    """

    sections = (
        role.strip(),
        _environment_section(config, workspace),
        _conventions_section(workspace),
    )
    return "\n\n".join(section for section in sections if section)


__all__ = ["build_subagent_prompt", "build_system_prompt", "load_builtin_prompt"]
