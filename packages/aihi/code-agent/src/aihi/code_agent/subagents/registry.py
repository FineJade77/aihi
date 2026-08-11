"""Named Subagent types owned by the Coding Agent application."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    """One delegatable role: its prompt, tool subset and capability ceiling."""

    name: str
    description: str
    prompt_file: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    tools: tuple[str, ...] | None = None
    model: str | None = None

    def prompt(self) -> str:
        """Read this type's packaged system prompt."""

        resource = files("aihi.code_agent.subagents") / "prompts" / self.prompt_file
        return resource.read_text(encoding="utf-8").strip()


__all__ = ["SubagentDefinition"]
