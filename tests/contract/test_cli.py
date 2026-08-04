import json
from pathlib import Path

from typer.testing import CliRunner

from aiharness.cli import app
from aiharness.sessions import Session, SQLiteEventStore

runner = CliRunner()


def test_new_creates_persistent_session(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    result = runner.invoke(
        app,
        ["new", "--cwd", str(tmp_path), "--db", str(database)],
    )

    assert result.exit_code == 0, result.stdout
    session_id = result.stdout.strip()
    store = SQLiteEventStore(database)
    try:
        session = Session.load(store, session_id)
        assert session.cwd == tmp_path.resolve()
    finally:
        store.close()


def test_run_requires_explicit_unsafe_host_and_then_streams_events(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    blocked = runner.invoke(
        app,
        ["run", "hello", "--cwd", str(tmp_path), "--db", str(database)],
    )
    assert blocked.exit_code != 0
    assert "unsafe=True" in blocked.output

    allowed = runner.invoke(
        app,
        [
            "run",
            "hello",
            "--cwd",
            str(tmp_path),
            "--db",
            str(database),
            "--unsafe-host",
        ],
    )
    assert allowed.exit_code == 0, allowed.stdout
    events = [json.loads(line) for line in allowed.stdout.splitlines()]
    assert any(event["type"] == "run.started" for event in events)
    started = next(event for event in events if event["type"] == "run.started")
    assert started["data"]["sandbox"] == "host"
    assert started["data"]["unsafe"] is True


def test_run_rejects_missing_workspace_with_stable_cli_error(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    missing = tmp_path / "does-not-exist"
    result = runner.invoke(
        app,
        [
            "run",
            "hello",
            "--cwd",
            str(missing),
            "--db",
            str(database),
            "--unsafe-host",
        ],
    )

    assert result.exit_code == 2
    assert "sandbox_invalid" in result.output


def test_events_and_resume_read_the_same_sqlite_session(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    run = runner.invoke(
        app,
        [
            "run",
            "first",
            "--cwd",
            str(tmp_path),
            "--db",
            str(database),
            "--unsafe-host",
        ],
    )
    assert run.exit_code == 0, run.stdout
    first_events = [json.loads(line) for line in run.stdout.splitlines()]
    session_id = next(event["session_id"] for event in first_events)

    resumed = runner.invoke(
        app,
        ["resume", session_id, "--db", str(database), "--unsafe-host"],
    )
    assert resumed.exit_code == 0, resumed.stdout
    assert any(json.loads(line)["type"] == "run.completed" for line in resumed.stdout.splitlines())

    listed = runner.invoke(app, ["events", session_id, "--db", str(database)])
    assert listed.exit_code == 0, listed.stdout
    all_events = [json.loads(line) for line in listed.stdout.splitlines()]
    assert len(all_events) > len(first_events)
    assert all_events[0]["type"] == "session.created"
