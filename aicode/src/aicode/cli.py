"""Minimal aicode CLI that composes the existing Harness runtime."""

# Typer's declarative signature intentionally uses ``Option`` calls as defaults.
# This mirrors the existing Harness CLI and is safe because Typer only inspects
# the values while building the command schema.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import typer

from aicode.app import build_runtime
from aicode.approvals import TerminalApprovalResolver
from aicode.config import AICodeConfig
from aiharness import (
    ApprovalResolver,
    EventInvariantViolation,
    Message,
    PermissionMode,
    RunCoordinator,
    RunResult,
    Session,
    SessionNotFound,
    SQLiteEventStore,
)

app = typer.Typer(no_args_is_help=True, help="Local Coding Agent composed from AIHarness.")

SUSPENDED_EXIT_CODE = 2


@app.command()
def run(
    prompt: str = typer.Argument(..., help="User task for the Coding Agent."),
    session: str | None = typer.Option(None, "--session", help="Existing session id."),
    workspace: Path | None = typer.Option(None, "--workspace", file_okay=False, dir_okay=True),
    db: Path | None = typer.Option(None, "--db"),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    unsafe_host: bool = typer.Option(
        False,
        "--unsafe-host",
        help="Explicitly acknowledge that Host execution is not isolated.",
    ),
    accept_edits: bool = typer.Option(
        False,
        "--accept-edits",
        help="Auto-allow workspace edits; running commands still needs approval.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Answer approval requests on this terminal instead of suspending the run.",
    ),
) -> None:
    """Run one prompt and print newly persisted events as JSON Lines."""

    config = AICodeConfig.from_env(workspace=workspace)
    overrides: dict[str, object] = {}
    # A CLI flag is an explicit acknowledgement. If it is omitted, preserve
    # the already validated environment/config value instead of silently
    # resetting it to the safe default.
    if unsafe_host:
        overrides["unsafe_host"] = True
    if provider is not None:
        overrides["provider"] = provider
    if model is not None:
        overrides["model"] = model
    if db is not None:
        overrides["db_path"] = db
    config = replace(config, **overrides)
    store = SQLiteEventStore(config.db_path)
    try:
        if session is None:
            current = Session.create(
                store,
                cwd=config.workspace,
                provider=config.provider,
                model=config.model,
            )
        else:
            current = _load_session(store, session)
            config = _resume_config(
                config,
                current,
                workspace_explicit=workspace is not None
                or os.getenv("AICODE_WORKSPACE") is not None,
                provider_explicit=provider is not None or os.getenv("AICODE_PROVIDER") is not None,
                model_explicit=model is not None or os.getenv("AICODE_MODEL") is not None,
            )
        before_seq = current.head_seq
        runtime = build_runtime(
            config, approval_resolver=_resolver(interactive), store=store
        )
        result = asyncio.run(
            runtime.coordinator.run(
                current,
                model=config.model,
                user_message=Message.text("user", prompt),
                permission_mode=(
                    PermissionMode.ACCEPT_EDITS if accept_edits else PermissionMode.DEFAULT
                ),
                system_prompt=runtime.system_prompt,
            )
        )
        _finish(current, result, before_seq=before_seq)
    finally:
        store.close()


@app.command()
def resume(
    session: str = typer.Argument(..., help="Session id to resume."),
    run_id: str | None = typer.Option(None, "--run", help="Suspended run id."),
    db: Path | None = typer.Option(None, "--db"),
    workspace: Path | None = typer.Option(None, "--workspace", file_okay=False, dir_okay=True),
    unsafe_host: bool = typer.Option(False, "--unsafe-host"),
    accept_edits: bool = typer.Option(False, "--accept-edits"),
    interactive: bool = typer.Option(False, "--interactive", "-i"),
) -> None:
    """Continue a run that was suspended waiting for an approval."""

    config = AICodeConfig.from_env(workspace=workspace)
    overrides: dict[str, object] = {}
    if unsafe_host:
        overrides["unsafe_host"] = True
    if db is not None:
        overrides["db_path"] = db
    config = replace(config, **overrides)
    store = SQLiteEventStore(config.db_path)
    try:
        current = _load_session(store, session)
        config = _resume_config(
            config,
            current,
            workspace_explicit=workspace is not None or os.getenv("AICODE_WORKSPACE") is not None,
            provider_explicit=os.getenv("AICODE_PROVIDER") is not None,
            model_explicit=os.getenv("AICODE_MODEL") is not None,
        )
        suspended = RunCoordinator.suspended_runs(current)
        target = run_id or (suspended[0] if suspended else None)
        if target is None:
            raise typer.BadParameter(
                "This session has no suspended run to resume", param_hint="--run"
            )
        before_seq = current.head_seq
        runtime = build_runtime(
            config, approval_resolver=_resolver(interactive), store=store
        )
        result = asyncio.run(
            runtime.coordinator.resume(
                current,
                run_id=target,
                model=config.model,
                permission_mode=(
                    PermissionMode.ACCEPT_EDITS if accept_edits else PermissionMode.DEFAULT
                ),
                system_prompt=runtime.system_prompt,
            )
        )
        _finish(current, result, before_seq=before_seq)
    finally:
        store.close()


@app.command()
def abandon(
    session: str = typer.Argument(..., help="Session id holding the suspended run."),
    run_id: str | None = typer.Option(None, "--run", help="Suspended run id."),
    db: Path | None = typer.Option(None, "--db"),
    reason: str = typer.Option("abandoned", "--reason"),
) -> None:
    """Give up on a suspended run instead of approving or denying it."""

    config = AICodeConfig.from_env()
    if db is not None:
        config = replace(config, db_path=db)
    store = SQLiteEventStore(config.db_path)
    try:
        current = _load_session(store, session)
        suspended = RunCoordinator.suspended_runs(current)
        target = run_id or (suspended[0] if suspended else None)
        if target is None:
            raise typer.BadParameter(
                "This session has no suspended run to abandon", param_hint="--run"
            )
        runtime = build_runtime(replace(config, workspace=current.cwd, unsafe_host=True))
        try:
            result = runtime.coordinator.abandon(current, run_id=target, reason=reason)
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--run") from error
        typer.echo(
            json.dumps({"run_id": result.run_id, "state": result.state.value}, sort_keys=True)
        )
    finally:
        store.close()


@app.command()
def approve(
    session: str = typer.Argument(..., help="Session id holding the pending approval."),
    approval_id: str = typer.Argument(..., help="Approval id reported by a suspended run."),
    db: Path | None = typer.Option(None, "--db"),
    deny: bool = typer.Option(False, "--deny", help="Reject the request instead of granting it."),
    once: bool = typer.Option(
        False, "--once", help="Allow only this call; the next one asks again."
    ),
    resolved_by: str = typer.Option("operator", "--by", help="Audit identity of the decision."),
) -> None:
    """Resolve a pending approval out of band, then resume the run separately."""

    config = AICodeConfig.from_env()
    if db is not None:
        config = replace(config, db_path=db)
    store = SQLiteEventStore(config.db_path)
    try:
        current = _load_session(store, session)
        pending = current.authorization.pending_approval(approval_id)
        if pending is None or pending.run_id is None:
            raise typer.BadParameter(
                f"No active pending approval: {approval_id}", param_hint="approval_id"
            )
        try:
            current.resolve_approval(
                approval_id,
                approved=not deny,
                resolved_by=resolved_by,
                run_id=pending.run_id,
                one_shot=once,
            )
        except EventInvariantViolation as error:
            raise typer.BadParameter(str(error), param_hint="approval_id") from error
        typer.echo(
            json.dumps(
                {
                    "approval_id": approval_id,
                    "status": "denied" if deny else "granted",
                    "one_shot": once and not deny,
                    "run_id": pending.run_id,
                    "scope": pending.scope,
                },
                sort_keys=True,
            )
        )
    finally:
        store.close()


def main() -> None:
    app()


def _resolver(interactive: bool) -> ApprovalResolver | None:
    return TerminalApprovalResolver() if interactive else None


def _load_session(store: SQLiteEventStore, session_id: str) -> Session:
    try:
        return Session.load(store, session_id)
    except SessionNotFound as error:
        raise typer.BadParameter(str(error), param_hint="--session") from error


def _finish(session: Session, result: RunResult, *, before_seq: int) -> None:
    """Print newly persisted events, then map the run outcome to an exit code."""

    for event in session.events:
        if (event.seq or 0) > before_seq:
            typer.echo(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
    if result.suspended:
        typer.echo(
            f"Run {result.run_id} is waiting for approval {result.pending_approval_id}.\n"
            f"Resolve it with: aicode approve {session.id} {result.pending_approval_id}\n"
            f"Then continue with: aicode resume {session.id} --run {result.run_id}",
            err=True,
        )
        raise typer.Exit(code=SUSPENDED_EXIT_CODE)
    if result.error is not None:
        raise typer.Exit(code=1)


def _resume_config(
    config: AICodeConfig,
    session: Session,
    *,
    workspace_explicit: bool,
    provider_explicit: bool,
    model_explicit: bool,
) -> AICodeConfig:
    """Restore persisted execution identity unless the caller explicitly overrides it."""

    if workspace_explicit and config.workspace != session.cwd:
        raise typer.BadParameter(
            "--workspace/AICODE_WORKSPACE must match the persisted session workspace",
            param_hint="--workspace",
        )
    stored_provider = session.metadata.get("provider")
    stored_model = session.metadata.get("model")
    if not provider_explicit and not isinstance(stored_provider, str):
        raise typer.BadParameter("Session metadata has no valid provider", param_hint="--session")
    if not model_explicit and not isinstance(stored_model, str):
        raise typer.BadParameter("Session metadata has no valid model", param_hint="--session")
    overrides: dict[str, object] = {"workspace": session.cwd}
    if not provider_explicit:
        overrides["provider"] = stored_provider
    if not model_explicit:
        overrides["model"] = stored_model
    return replace(config, **overrides)


__all__ = ["app", "main"]
