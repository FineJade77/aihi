"""Inject the Skill *index* into a compiled context.

Only metadata is offered to the model: names, versions, scopes and one-line
descriptions. Bodies stay behind the explicit request, trust-policy and re-hash flow in
`SkillLoader`, so a discovered skill can never widen a run's knowledge — let
alone its permissions — without a deliberate request (ARCHITECTURE §9.3).
"""

from __future__ import annotations

from aihi.agent.context import ContextSection
from aihi.agent.skills.discovery import SkillCandidate, SkillDiscovery

_TITLE = "Available skills"


def render_index(
    candidates: tuple[SkillCandidate, ...], *, load_tool_name: str | None = None
) -> str:
    lines = [
        f"- {candidate.frontmatter.name}@{candidate.frontmatter.version} "
        f"({candidate.scope.value}): {candidate.frontmatter.description}"
        for candidate in candidates
    ]
    if not lines:
        return ""
    if load_tool_name is None:
        lines.append(
            "Request a skill body explicitly before relying on it; the index alone "
            "grants no tools or permissions."
        )
    else:
        lines.append(
            f"Load one with the {load_tool_name} tool, passing the exact name@version "
            "shown above, before relying on it; the index alone grants no tools "
            "or permissions."
        )
    return "\n".join(lines)


class SkillIndexContributor:
    """Contribute the discovered skill index, never the Markdown bodies."""

    def __init__(
        self,
        discovery: SkillDiscovery,
        *,
        limit: int = 50,
        load_tool_name: str | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("Skill index limit must be positive")
        if load_tool_name is not None and not load_tool_name.strip():
            raise ValueError("Skill load tool name must be non-empty")
        self.discovery = discovery
        self.limit = limit
        self.load_tool_name = load_tool_name

    def sections(self, request: object) -> tuple[ContextSection, ...]:
        candidates = self.discovery.discover()[: self.limit]
        body = render_index(candidates, load_tool_name=self.load_tool_name)
        if not body:
            return ()
        return (ContextSection(title=_TITLE, body=body, source="skills"),)


__all__ = ["SkillIndexContributor", "render_index"]
