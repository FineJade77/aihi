"""Run a shell command through bash.

The command is a string because that is what models actually produce, and
because pipes, redirection and `&&` are ordinary parts of the work. bash is
exec'd explicitly with the script as an argument — the sandbox never uses
`shell=True`, so there is no second round of parsing.

Safety here does **not** come from inspecting the command. It comes from:

- `process.exec`, which makes every call require an explicit approval, so a
  human reads the command before it runs (ADR-0028);
- the sandbox: workspace root, timeout, output caps, process-group cleanup.

The sensitive-path rule in `DefaultPolicyEngine` still fires on the obvious
forms, but it is a heuristic, not a boundary: quoting defeats it.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

from aihi.agent import (
    PreparedToolCall,
    SandboxBackend,
    SandboxViolation,
    ToolContext,
    ToolExecutionResult,
    ToolInputError,
    ToolSpec,
)

from .command import format_command_result

MAX_COMMAND_LENGTH = 16_384


def resolve_bash(shell_path: str | Path | None = None) -> str:
    """Locate bash up front so a missing interpreter fails at construction."""

    if shell_path is not None:
        candidate = Path(shell_path)
        if not candidate.is_file():
            raise SandboxViolation(f"Configured shell is not a file: {candidate}")
        return str(candidate)
    found = shutil.which("bash")
    if found is None:
        raise SandboxViolation("bash was not found on PATH; pass shell_path explicitly")
    return found


class BashTool:
    spec = ToolSpec.define(
        name="bash",
        description=(
            "Run a shell command with bash in the workspace. Pipes, redirection and && "
            "work. Each call runs in its own shell: cd does not carry over, so chain "
            "steps with && instead. Prefer glob and grep for read-only searching - they "
            "need no approval and can run in parallel."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_output_chars": {"type": "integer"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
        mutates=True,
        required_capabilities=("process.exec",),
        timeout_seconds=120.0,
    )

    def __init__(
        self, sandbox: SandboxBackend, *, shell_path: str | Path | None = None
    ) -> None:
        self.sandbox = sandbox
        self.shell_path = resolve_bash(shell_path)

    def prepare(
        self, input: dict[str, Any], context: ToolContext[Any]
    ) -> PreparedToolCall:
        del context
        return PreparedToolCall(
            self._normalized_input(input),
            {
                "transport": "sandbox",
                "sandbox": self.sandbox.descriptor.to_dict(),
                "cwd": str(self.sandbox.root),
            },
        )

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        del context
        normalized = self._normalized_input(input)
        command = normalized["command"]
        timeout_seconds = normalized["timeout_seconds"]
        max_output_chars = normalized["max_output_chars"]
        result = await self.sandbox.run_command(
            (self.shell_path, "-c", command),
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        return format_command_result(result, label="bash", metadata={"command": command})

    def _normalized_input(self, input: dict[str, Any]) -> dict[str, Any]:
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolInputError("command must be a non-empty string")
        if len(command) > MAX_COMMAND_LENGTH:
            raise ToolInputError(f"command exceeds {MAX_COMMAND_LENGTH} characters")
        timeout_seconds = float(input.get("timeout_seconds", self.spec.timeout_seconds))
        max_output_chars = int(input.get("max_output_chars", 100_000))
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_chars <= 0:
            raise ToolInputError("timeout_seconds and max_output_chars must be positive")
        return {
            "command": command,
            "timeout_seconds": timeout_seconds,
            "max_output_chars": max_output_chars,
        }


__all__ = ["MAX_COMMAND_LENGTH", "BashTool", "resolve_bash"]
