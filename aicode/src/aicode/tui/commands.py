"""Slash commands: the things you tell the front end, not the model.

Kept apart from the chat loop so the split stays visible — a slash command
changes how *this terminal* behaves, and must never be quietly forwarded to the
model as a prompt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiharness import PermissionMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aicode.tui.chat import ChatLoop


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    handler: Callable[[ChatLoop, str], Awaitable[bool]]
    usage: str = ""


async def _help(loop: ChatLoop, _: str) -> bool:
    width = max(len(command.name + command.usage) for command in COMMANDS) + 2
    loop.console.line()
    for command in COMMANDS:
        label = f"{command.name} {command.usage}".strip()
        loop.console.line(f"  {label:<{width}} {command.summary}", loop.console.palette.dim)
    loop.console.line()
    loop.console.line("  esc or ctrl-c interrupts a running turn", loop.console.palette.dim)
    return False


async def _exit(loop: ChatLoop, _: str) -> bool:
    return True


async def _clear(loop: ChatLoop, _: str) -> bool:
    """Start a new session rather than editing this one.

    The event log is append-only: forgetting is a new log, never a rewrite of
    the old one, which stays on disk and replayable.
    """

    loop.start_session()
    loop.console.notice(f"New session {loop.session.id}. The previous one is still on disk.")
    return False


async def _mode(loop: ChatLoop, argument: str) -> bool:
    if not argument:
        loop.console.notice(f"Permission mode: {loop.permission_mode.value}")
        return False
    wanted = argument.strip().lower().replace("-", "_")
    try:
        loop.permission_mode = PermissionMode(wanted)
    except ValueError:
        allowed = ", ".join(mode.value for mode in PermissionMode)
        loop.console.notice(f"Unknown mode {argument!r}. Try one of: {allowed}")
        return False
    if loop.permission_mode is PermissionMode.BYPASS:
        loop.console.line(
            "  Bypass mode: tool calls are no longer reviewed.", loop.console.palette.red
        )
    else:
        loop.console.notice(f"Permission mode: {loop.permission_mode.value}")
    return False


async def _model(loop: ChatLoop, argument: str) -> bool:
    if not argument:
        loop.console.notice(f"Model: {loop.model}")
        return False
    loop.model = argument.strip()
    loop.console.notice(f"Model: {loop.model}")
    return False


async def _tools(loop: ChatLoop, _: str) -> bool:
    loop.console.line()
    room = loop.console.width - 24
    for spec in sorted(loop.runtime.registry.specs, key=lambda item: item.name):
        marker = "writes" if spec.mutates else "reads"
        summary = " ".join(spec.description.split())
        if len(summary) > room:
            summary = summary[: max(12, room - 1)] + "…"
        loop.console.line(f"  {spec.name:<12} {marker:<7} {summary}", loop.console.palette.dim)
    return False


async def _usage(loop: ChatLoop, _: str) -> bool:
    total = loop.usage
    loop.console.notice(
        f"{total.input_tokens} in · {total.output_tokens} out · "
        f"{total.cached_input_tokens} cached, over {loop.turns} turn(s)"
    )
    return False


async def _session(loop: ChatLoop, _: str) -> bool:
    loop.session.refresh()
    loop.console.notice(
        f"{loop.session.id} · {loop.config.workspace} · {loop.session.head_seq} events · "
        f"{loop.model} · {loop.permission_mode.value}"
    )
    return False


async def _resume(loop: ChatLoop, argument: str) -> bool:
    await loop.resume(argument.strip() or None)
    return False


async def _config(loop: ChatLoop, _: str) -> bool:
    """Re-run the setup questions and adopt the answers without restarting."""

    from aicode.tui.setup import ensure_configured

    updated = await ensure_configured(loop.console, loop.config, force=True)
    if updated is not None:
        loop.reconfigure(updated)
    return False


async def _thinking(loop: ChatLoop, _: str) -> bool:
    showing = loop.toggle_thinking()
    loop.console.notice(f"Thinking output: {'on' if showing else 'off'}")
    return False


COMMANDS: tuple[Command, ...] = (
    Command("/help", "show this list", _help),
    Command("/clear", "start a fresh session", _clear),
    Command("/config", "change provider, model or API key", _config),
    Command("/mode", "show or set the permission mode", _mode, "[plan|default|accept-edits]"),
    Command("/model", "show or set the model", _model, "[name]"),
    Command("/tools", "list the tools the model can call", _tools),
    Command("/usage", "token totals for this session", _usage),
    Command("/session", "session id, workspace and event count", _session),
    Command("/resume", "continue a run that is waiting for approval", _resume, "[run_id]"),
    Command("/thinking", "show or hide the model's reasoning", _thinking),
    Command("/exit", "leave (ctrl-d works too)", _exit),
)

_BY_NAME = {command.name: command for command in COMMANDS}
_ALIASES = {"/quit": "/exit", "/q": "/exit", "/?": "/help"}

COMMAND_NAMES: tuple[str, ...] = tuple(sorted({*_BY_NAME, *_ALIASES}))


async def dispatch(loop: ChatLoop, line: str) -> bool:
    """Run one slash command. Returns True when the loop should stop."""

    head, _, argument = line.partition(" ")
    name = _ALIASES.get(head, head)
    command = _BY_NAME.get(name)
    if command is None:
        loop.console.notice(f"Unknown command {head}. Try /help.")
        return False
    return await command.handler(loop, argument.strip())


__all__ = ["COMMAND_NAMES", "COMMANDS", "Command", "dispatch"]
