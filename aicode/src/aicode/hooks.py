"""Run the project's formatter after the agent edits a file.

A hook that runs a command is a side effect, so it plays by the Harness rules
rather than around them:

- it registers as `mutates=True`, which the bus only accepts together with
  explicit trust — configuring `AICODE_FORMAT_COMMAND` *is* that act of trust;
- it runs only when the enclosing tool call was allowed by policy and the
  sandbox is acknowledged. A hook cannot mint that evidence, it can only read
  the `HookGovernance` the dispatcher passes in;
- it executes through the sandbox, so workspace confinement, timeout and output
  caps apply exactly as they do to `bash`;
- it never fails the run. A formatter that errors is reported, not fatal: the
  edit the agent made is already committed and correct.
"""

from __future__ import annotations

import shlex
from typing import Any

from aiharness import HookBus, HookEvent, SandboxBackend

EDIT_TOOLS = frozenset({"edit_file", "write_file"})
DEFAULT_TIMEOUT_SECONDS = 30.0


class FormatOnEditHook:
    """Format a file right after the agent writes it."""

    def __init__(
        self,
        command: str,
        sandbox: SandboxBackend,
        *,
        shell_path: str,
        tools: frozenset[str] = EDIT_TOOLS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not command.strip():
            raise ValueError("format command must be a non-empty string")
        if timeout_seconds <= 0:
            raise ValueError("format timeout must be positive")
        self.command = command.strip()
        self.sandbox = sandbox
        self.shell_path = shell_path
        self.tools = tools
        self.timeout_seconds = timeout_seconds
        self.runs: list[str] = []

    async def __call__(self, event: HookEvent) -> dict[str, object] | None:
        payload: Any = event.payload
        governance = event.governance
        if payload.get("tool_name") not in self.tools:
            return None
        if payload.get("is_error"):
            return None
        if governance is None or not governance.allows_mutation:
            # The enclosing call was not allowed, or the sandbox is not
            # acknowledged. Formatting is not an excuse to act anyway.
            return {"skipped": "not_authorized"}
        path = (payload.get("input") or {}).get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        command = f"{self.command} {shlex.quote(path)}"
        self.runs.append(command)
        result = await self.sandbox.run_command(
            (self.shell_path, "-c", command),
            timeout_seconds=self.timeout_seconds,
            max_output_chars=10_000,
        )
        return {
            "formatted": path,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
        }


def register_format_hook(
    bus: HookBus, hook: FormatOnEditHook, *, event_name: str = "tool.after"
) -> str:
    """Register the formatter as a trusted mutating hook."""

    return bus.register(
        event_name,
        hook,
        hook_id="aicode.format_on_edit",
        mutates=True,
        # Trust comes from the operator configuring the command, not from the
        # model or the project tree.
        trusted=True,
        source="aicode.config",
        timeout_seconds=hook.timeout_seconds + 5.0,
    )


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "EDIT_TOOLS", "FormatOnEditHook", "register_format_hook"]
