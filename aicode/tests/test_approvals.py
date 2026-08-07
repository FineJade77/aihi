"""Terminal approval UX and the suspend / approve / resume CLI loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aicode.approvals import TerminalApprovalResolver
from aicode.cli import SUSPENDED_EXIT_CODE, app
from typer.testing import CliRunner

from aicode import app as app_module
from aiharness.models.providers.fake import FakeProvider, FakeStep
from aiharness.policy import ApprovalOutcome, ApprovalRequest

runner = CliRunner()

WRITE_CALL = {"path": "note.txt", "content": "approved"}


def request_for(tool: str = "write_file") -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-1",
        session_id="ses-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool,
        tool_input=dict(WRITE_CALL),
        reason="This tool can mutate external state and requires approval.",
        rule_id="default.mutation_requires_approval",
        required_capabilities=("filesystem.write",),
        sandbox={"name": "host", "unsafe": True},
    )


def resolve(answer: str | BaseException) -> ApprovalOutcome:
    written: list[str] = []

    def reader() -> str:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    resolver = TerminalApprovalResolver(reader=reader, writer=written.append)
    outcome = resolver._ask(request_for())
    assert "write_file" in written[0]
    assert "unsafe=True" in written[0]
    return outcome


def script(monkeypatch: pytest.MonkeyPatch, steps: list[FakeStep]) -> None:
    """Drive the real composition root with a scripted provider."""

    monkeypatch.setattr(app_module, "build_provider", lambda config: FakeProvider(list(steps)))


def events_of(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_terminal_resolver_maps_answers_and_defers_by_default() -> None:
    # The product default is a single call, not the whole run.
    assert resolve("y") == ApprovalOutcome.GRANTED_ONCE
    assert resolve("YES") == ApprovalOutcome.GRANTED_ONCE
    assert resolve("a") == ApprovalOutcome.GRANTED
    assert resolve("n") == ApprovalOutcome.DENIED
    assert resolve("") == ApprovalOutcome.DEFERRED
    assert resolve("maybe") == ApprovalOutcome.DEFERRED
    # An unanswerable prompt must never be read as consent.
    assert resolve(EOFError()) == ApprovalOutcome.DEFERRED
    assert resolve(KeyboardInterrupt()) == ApprovalOutcome.DEFERRED


def test_cli_suspends_then_approves_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    common = ["--db", str(database), "--workspace", str(workspace), "--unsafe-host"]
    script(
        monkeypatch,
        [FakeStep.call_tool("write_file", WRITE_CALL), FakeStep(text="done")],
    )

    started = runner.invoke(app, ["run", "write a note", *common])

    assert started.exit_code == SUSPENDED_EXIT_CODE, started.output
    events = events_of(started.output)
    session_id = events[0]["session_id"]
    suspended = next(event for event in events if event["type"] == "run.suspended")
    approval_id = suspended["data"]["approval_id"]
    run_id = suspended["run_id"]
    assert not (workspace / "note.txt").exists()
    assert "aicode approve" in started.output

    granted = runner.invoke(app, ["approve", session_id, approval_id, "--db", str(database)])
    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.stdout)["status"] == "granted"

    script(monkeypatch, [FakeStep(text="done")])
    resumed = runner.invoke(app, ["resume", session_id, "--run", run_id, *common])

    assert resumed.exit_code == 0, resumed.output
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "approved"
    resumed_events = events_of(resumed.output)
    assert any(event["type"] == "run.resumed" for event in resumed_events)
    assert any(event["type"] == "run.completed" for event in resumed_events)


def test_cli_interactive_run_grants_without_suspending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script(
        monkeypatch,
        [FakeStep.call_tool("write_file", WRITE_CALL), FakeStep(text="done")],
    )

    result = runner.invoke(
        app,
        [
            "run",
            "write a note",
            "--db",
            str(database),
            "--workspace",
            str(workspace),
            "--unsafe-host",
            "--interactive",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "approved"
    resolved = next(
        event for event in events_of(result.output) if event["type"] == "approval.resolved"
    )
    assert resolved["data"]["status"] == "granted"
    assert resolved["data"]["resolved_by"] == "terminal"


def test_cli_accept_edits_still_suspends_before_running_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "executed.txt"
    script(
        monkeypatch,
        [
            FakeStep.call_tool("bash", {"command": f"touch {marker}"}),
            FakeStep(text="done"),
        ],
    )

    result = runner.invoke(
        app,
        [
            "run",
            "run a command",
            "--db",
            str(database),
            "--workspace",
            str(workspace),
            "--unsafe-host",
            "--accept-edits",
        ],
    )

    assert result.exit_code == SUSPENDED_EXIT_CODE, result.output
    assert not marker.exists()
    decision = next(
        event for event in events_of(result.output) if event["type"] == "policy.decided"
    )
    assert decision["data"]["rule_id"] == "default.execution_requires_approval"


def test_cli_rejects_resume_and_approve_without_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script(monkeypatch, [FakeStep(text="nothing to do")])
    started = runner.invoke(
        app,
        ["run", "hello", "--db", str(database), "--workspace", str(workspace), "--unsafe-host"],
    )
    assert started.exit_code == 0, started.output
    session_id = events_of(started.output)[0]["session_id"]

    missing_run = runner.invoke(app, ["resume", session_id, "--db", str(database), "--unsafe-host"])
    missing_approval = runner.invoke(
        app, ["approve", session_id, "approval-missing", "--db", str(database)]
    )

    assert missing_run.exit_code != 0
    assert "no suspended run" in missing_run.output
    assert missing_approval.exit_code != 0
    assert "No active pending approval" in missing_approval.output


def test_cli_can_abandon_a_suspended_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    common = ["--db", str(database), "--workspace", str(workspace), "--unsafe-host"]
    script(
        monkeypatch,
        [FakeStep.call_tool("write_file", WRITE_CALL), FakeStep(text="done")],
    )
    started = runner.invoke(app, ["run", "write a note", *common])
    assert started.exit_code == SUSPENDED_EXIT_CODE, started.output
    events = events_of(started.output)
    session_id = events[0]["session_id"]
    run_id = next(e for e in events if e["type"] == "run.suspended")["run_id"]

    result = runner.invoke(
        app, ["abandon", session_id, "--run", run_id, "--db", str(database), "--reason", "nope"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "cancelled"
    assert not (workspace / "note.txt").exists()
    # A resumed run is no longer offered.
    again = runner.invoke(app, ["resume", session_id, *common])
    assert again.exit_code != 0
    assert "no suspended run" in again.output


def test_cli_can_list_and_inspect_persisted_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script(monkeypatch, [FakeStep(text="hello")])
    started = runner.invoke(
        app,
        ["run", "hi", "--db", str(database), "--workspace", str(workspace), "--unsafe-host"],
    )
    assert started.exit_code == 0, started.output
    session_id = events_of(started.output)[0]["session_id"]

    listed = runner.invoke(app, ["sessions", "--db", str(database)])
    dumped = runner.invoke(app, ["events", session_id, "--db", str(database)])

    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout.splitlines()[0])["session_id"] == session_id
    assert dumped.exit_code == 0, dumped.output
    types = [json.loads(line)["type"] for line in dumped.stdout.splitlines()]
    assert types[0] == "session.created"
    assert "run.completed" in types
