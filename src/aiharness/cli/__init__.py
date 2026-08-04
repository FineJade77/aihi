"""Typer command-line entry point for local sessions and event streams."""

# Typer's command signatures intentionally use Option/Argument factories.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from aiharness.core.errors import SandboxViolation, SessionNotFound, UnsafeHostNotAcknowledged
from aiharness.core.types import Message
from aiharness.models.providers.fake import FakeProvider
from aiharness.runtime import RunCoordinator
from aiharness.sandbox import HostBackend
from aiharness.sessions import Session, SQLiteEventStore
from aiharness.tools import ToolRegistry
from aiharness.tools.builtin import ReadFileTool

app = typer.Typer(no_args_is_help=True, help="AIHarness local session and event-stream CLI.")


def _store(path: Path) -> SQLiteEventStore:
    return SQLiteEventStore(path)


def _print_events(session: Session, *, after_seq: int) -> None:
    for event in session.events:
        if (event.seq or 0) > after_seq:
            typer.echo(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))


def _load_or_create(
    store: SQLiteEventStore,
    *,
    session_id: str | None,
    cwd: Path,
    model: str,
) -> Session:
    if session_id is not None:
        try:
            return Session.load(store, session_id)
        except SessionNotFound as error:
            raise typer.BadParameter(str(error), param_hint="--session") from error
    return Session.create(store, cwd=cwd, provider="fake", model=model)


def _coordinator(session: Session, *, unsafe_host: bool) -> RunCoordinator:
    try:
        sandbox = HostBackend(session.cwd, unsafe=unsafe_host)
    except UnsafeHostNotAcknowledged as error:
        raise typer.BadParameter(str(error), param_hint="--unsafe-host") from error
    except (FileNotFoundError, SandboxViolation) as error:
        code = getattr(error, "code", "sandbox_invalid")
        raise typer.BadParameter(f"{code}: {error}", param_hint="--cwd") from error
    return RunCoordinator(
        FakeProvider(),
        registry=ToolRegistry([ReadFileTool()]),
        sandbox=sandbox,
    )


@app.command()
def new(
    cwd: Path | None = typer.Option(None, "--cwd", file_okay=False, dir_okay=True),
    db: Path = typer.Option(Path(".aiharness/events.db"), "--db"),
    model: str = typer.Option("fake-model", "--model"),
) -> None:
    """Create a persistent SQLite-backed session and print its id."""

    store = _store(db)
    try:
        session = Session.create(store, cwd=cwd or Path.cwd(), provider="fake", model=model)
        typer.echo(session.id)
    finally:
        store.close()


@app.command()
def run(
    prompt: str = typer.Argument(..., help="User prompt for the Fake Provider."),
    session: str | None = typer.Option(None, "--session", help="Existing session id."),
    cwd: Path | None = typer.Option(None, "--cwd", file_okay=False, dir_okay=True),
    db: Path = typer.Option(Path(".aiharness/events.db"), "--db"),
    model: str = typer.Option("fake-model", "--model"),
    unsafe_host: bool = typer.Option(
        False,
        "--unsafe-host",
        help="Acknowledge that Host execution is not isolated.",
    ),
) -> None:
    """Run a prompt and print newly persisted events as JSON lines."""

    store = _store(db)
    try:
        current = _load_or_create(
            store, session_id=session, cwd=cwd or Path.cwd(), model=model
        )
        before_seq = current.head_seq
        coordinator = _coordinator(current, unsafe_host=unsafe_host)
        result = asyncio.run(
            coordinator.run(
                current,
                model=model,
                user_message=Message.text("user", prompt),
            )
        )
        _print_events(current, after_seq=before_seq)
        if result.error is not None:
            raise typer.Exit(code=1)
    finally:
        store.close()


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session id to resume."),
    db: Path = typer.Option(Path(".aiharness/events.db"), "--db"),
    model: str | None = typer.Option(None, "--model"),
    unsafe_host: bool = typer.Option(False, "--unsafe-host"),
) -> None:
    """Resume a persisted session and print newly appended events."""

    store = _store(db)
    try:
        current = _load_or_create(
            store,
            session_id=session_id,
            cwd=Path.cwd(),
            model=model or "fake-model",
        )
        before_seq = current.head_seq
        coordinator = _coordinator(current, unsafe_host=unsafe_host)
        result = asyncio.run(
            coordinator.resume(
                current,
                model=model or str(current.metadata.get("model", "fake-model")),
            )
        )
        _print_events(current, after_seq=before_seq)
        if result.error is not None:
            raise typer.Exit(code=1)
    finally:
        store.close()


@app.command()
def events(
    session_id: str = typer.Argument(..., help="Session id to inspect."),
    db: Path = typer.Option(Path(".aiharness/events.db"), "--db"),
) -> None:
    """Print all persisted events for a session as JSON lines."""

    store = _store(db)
    try:
        try:
            session = Session.load(store, session_id)
        except SessionNotFound as error:
            raise typer.BadParameter(str(error), param_hint="session_id") from error
        for event in session.events:
            typer.echo(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
    finally:
        store.close()


def main() -> None:
    app()


__all__ = ["app", "main"]
