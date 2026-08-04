"""Direct argv command tool; no shell string interpolation is permitted."""

from __future__ import annotations

from aiharness.core.types import ToolSpec
from aiharness.tools.base import ToolContext, ToolResult
from aiharness.tools.builtin.command import execute_argv


class ShellTool:
    spec = ToolSpec(
        name="shell",
        description="Run an argv command in the active workspace without a shell parser.",
        input_schema={
            "type": "object",
            "properties": {
                "argv": {"type": "array"},
                "timeout_seconds": {"type": "number"},
                "max_output_chars": {"type": "integer"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
        mutates=True,
        required_capabilities=("process.exec",),
        timeout_seconds=120.0,
    )

    async def run(self, input: dict[str, object], context: ToolContext) -> ToolResult:
        return await execute_argv(
            input,
            context,
            default_timeout=self.spec.timeout_seconds,
            default_max_output=100_000,
            label="shell",
        )
