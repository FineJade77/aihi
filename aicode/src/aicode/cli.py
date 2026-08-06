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
from aicode.config import AICodeConfig
from aiharness.core.errors import SessionNotFound
from aiharness.core.types import Message
from aiharness.policy import PermissionMode
from aiharness.sessions import Session, SQLiteEventStore

app = typer.Typer(no_args_is_help=True, help="Local Coding Agent composed from AIHarness.")


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
        help="Allow mutating tools for this local run; approval UX is still application work.",
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
            try:
                current = Session.load(store, session)
            except SessionNotFound as exc:
                raise typer.BadParameter(str(exc), param_hint="--session") from exc
            config = _resume_config(
                config,
                current,
                workspace_explicit=workspace is not None
                or os.getenv("AICODE_WORKSPACE") is not None,
                provider_explicit=provider is not None or os.getenv("AICODE_PROVIDER") is not None,
                model_explicit=model is not None or os.getenv("AICODE_MODEL") is not None,
            )
        before_seq = current.head_seq
        runtime = build_runtime(config)
        result = asyncio.run(
            runtime.coordinator.run(
                current,
                model=config.model,
                user_message=Message.text("user", prompt),
                permission_mode=(
                    PermissionMode.ACCEPT_EDITS if accept_edits else PermissionMode.DEFAULT
                ),
            )
        )
        for event in current.events:
            if (event.seq or 0) > before_seq:
                typer.echo(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        if result.error is not None:
            raise typer.Exit(code=1)
    finally:
        store.close()


def main() -> None:
    app()


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
