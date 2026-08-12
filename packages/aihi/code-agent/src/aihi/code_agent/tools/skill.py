"""Application-owned Skill tools for explicit, trusted body loading."""

from __future__ import annotations

from typing import Any

from aihi.agent.skills import SkillLoader
from aihi.agent.tools import ToolContext, ToolExecutionResult, ToolSpec


class LoadSkillTool:
    """Load one Skill body after the shared trust and integrity checks."""

    spec = ToolSpec.define(
        name="load_skill",
        description=(
            "Load the full body of a Skill by name or exact name@version from "
            "the discovered catalog. Builtin Skills are trusted with the package; "
            "other scopes must be explicitly trusted and enabled in the lockfile."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        concurrency_safe=True,
        mutates=False,
        required_capabilities=("filesystem.read",),
        timeout_seconds=10.0,
    )

    def __init__(self, loader: SkillLoader) -> None:
        self.loader = loader

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
        name = input.get("name")
        if not isinstance(name, str) or not name.strip():
            return ToolExecutionResult(
                content="Skill name must be a non-empty string.",
                is_error=True,
                metadata={"error_code": "skill_name_invalid"},
            )
        loaded = self.loader.load_by_name(name.strip(), requested=True)
        return ToolExecutionResult(
            content=(
                f"# Skill: {loaded.name}\n"
                f"Version: {loaded.version}\n"
                f"Scope: {loaded.scope.value}\n\n"
                f"{loaded.body}"
            ),
            metadata={
                "skill_name": loaded.name,
                "skill_version": loaded.version,
                "skill_scope": loaded.scope.value,
                "content_sha256": loaded.content_sha256,
            },
        )


__all__ = ["LoadSkillTool"]
