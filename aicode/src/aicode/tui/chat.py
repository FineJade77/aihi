"""The interactive loop: read a line, run a turn, render what happened.

Everything stateful lives in the Harness session, not here. This object holds
only what belongs to *this terminal* — the permission mode, which model the
next turn uses, whether reasoning is shown — so closing the window loses
nothing that mattered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from aicode.app import AICodeRuntime, build_runtime
from aicode.config import AICodeConfig
from aicode.tui.approve import ConsoleApprovalResolver
from aicode.tui.commands import COMMAND_NAMES, dispatch
from aicode.tui.console import Console
from aicode.tui.input import PromptReader
from aicode.tui.keys import interrupt_watch
from aicode.tui.render import TranscriptRenderer
from aiharness import (
    EventStore,
    Message,
    PermissionMode,
    Provider,
    RunCoordinator,
    RunResult,
    RunState,
    Session,
    Usage,
)

_HISTORY = Path(".aiharness") / "history"


class ChatLoop:
    """One terminal, one workspace, one persistent session at a time."""

    def __init__(
        self,
        config: AICodeConfig,
        store: EventStore,
        console: Console,
        *,
        session: Session | None = None,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        resolver: ConsoleApprovalResolver | None = None,
        provider: Provider | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.console = console
        self.model = config.model
        self.permission_mode = permission_mode
        self.usage = Usage()
        self.turns = 0
        self.resolver = resolver or ConsoleApprovalResolver(console, workspace=config.workspace)
        self.runtime: AICodeRuntime = build_runtime(
            config, approval_resolver=self.resolver, store=store, provider=provider
        )
        self.renderer = TranscriptRenderer(console, workspace=config.workspace)
        self.session = session if session is not None else self._new_session()
        self.session.add_event_observer(self.renderer.observe)

    # --- loop ------------------------------------------------------------

    async def run(self) -> int:
        """Read and answer until EOF or `/exit`. Returns a process exit code."""

        self._banner()
        reader = PromptReader(
            history_path=self.config.workspace / _HISTORY, commands=COMMAND_NAMES
        )
        while True:
            try:
                line = await reader.read(self._prompt())
            except asyncio.CancelledError:
                break
            if line is None:
                break
            if not line:
                continue
            if line.startswith("/"):
                if await dispatch(self, line):
                    break
                continue
            await self.turn(line)
        self.console.clear_status()
        self.console.line()
        return 0

    async def turn(self, text: str) -> RunResult:
        """One user instruction, start to finish, interruptible throughout."""

        self.turns += 1
        self.renderer.begin_turn()
        cancel = asyncio.Event()
        async with interrupt_watch(cancel, on_first=self._announce_interrupt) as interrupts:
            self.resolver.interrupts = interrupts
            try:
                result = await self.runtime.coordinator.run(
                    self.session,
                    model=self.model,
                    user_message=Message.text("user", text),
                    permission_mode=self.permission_mode,
                    system_prompt=self.runtime.system_prompt,
                    cancel_event=cancel,
                )
            finally:
                self.resolver.interrupts = None
        self._settle(result)
        return result

    async def resume(self, run_id: str | None) -> RunResult | None:
        """Continue a run that suspended on an approval."""

        self.session.refresh()
        suspended = RunCoordinator.suspended_runs(self.session)
        target = run_id or (suspended[0] if suspended else None)
        if target is None:
            self.console.notice("No suspended run in this session.")
            return None
        self.renderer.begin_turn()
        cancel = asyncio.Event()
        async with interrupt_watch(cancel, on_first=self._announce_interrupt) as interrupts:
            self.resolver.interrupts = interrupts
            try:
                result = await self.runtime.coordinator.resume(
                    self.session,
                    run_id=target,
                    model=self.model,
                    permission_mode=self.permission_mode,
                    system_prompt=self.runtime.system_prompt,
                    cancel_event=cancel,
                )
            finally:
                self.resolver.interrupts = None
        self._settle(result)
        return result

    # --- state the terminal owns -----------------------------------------

    def start_session(self) -> Session:
        self.session = self._new_session()
        self.session.add_event_observer(self.renderer.observe)
        self.usage = Usage()
        self.turns = 0
        return self.session

    def toggle_thinking(self) -> bool:
        self.renderer.show_thinking = not self.renderer.show_thinking
        return self.renderer.show_thinking

    def reconfigure(self, config: AICodeConfig) -> None:
        """Adopt new settings mid-session by rebuilding the runtime.

        The session is untouched: switching provider does not rewrite what has
        already happened, it only changes who answers next.
        """

        self.config = config
        self.model = config.model
        self.runtime = build_runtime(
            config, approval_resolver=self.resolver, store=self.store
        )

    # --- internals -------------------------------------------------------

    def _new_session(self) -> Session:
        return Session.create(
            self.store,
            cwd=self.config.workspace,
            provider=self.config.provider,
            model=self.model,
        )

    def _settle(self, result: RunResult) -> None:
        self.console.clear_status()
        self.console.ensure_line_start()
        if result.response is not None:
            self.usage = _add(self.usage, result.response.usage)
        palette = self.console.palette
        if result.suspended:
            self.console.line(
                f"  Waiting for approval {result.pending_approval_id}. "
                f"Continue here with /resume, or from another terminal with "
                f"`aicode approve {self.session.id} {result.pending_approval_id}`.",
                palette.yellow,
            )
        elif result.state is RunState.INTERRUPTED:
            self.console.line("  Interrupted.", palette.yellow)
        elif result.error is not None:
            self.console.line(f"  {result.error}", palette.red)
        self.console.line()

    def _announce_interrupt(self) -> None:
        self.console.clear_status()
        self.console.notice("Interrupting after the current step…")

    def _banner(self) -> None:
        palette = self.console.palette
        self.console.line()
        self.console.line(f"  aicode {palette.paint('·', palette.dim)} {self.model}", palette.bold)
        self.console.line(f"  {self.config.workspace}", palette.dim)
        if self.runtime.sandbox.descriptor.unsafe:
            self.console.line(
                "  Host execution is not isolated: tools act directly on this machine.",
                palette.red,
            )
        self.console.line("  /help for commands, ctrl-d to leave", palette.dim)
        self.console.line()

    def _prompt(self) -> str:
        mode = "" if self.permission_mode is PermissionMode.DEFAULT else (
            f" [{self.permission_mode.value}]"
        )
        return f"{mode}› "


def _add(total: Usage, delta: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + delta.input_tokens,
        output_tokens=total.output_tokens + delta.output_tokens,
        cached_input_tokens=total.cached_input_tokens + delta.cached_input_tokens,
    )


__all__ = ["ChatLoop"]
