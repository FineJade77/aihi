"""The transcript renderer turns events into something a human reads."""

from __future__ import annotations

import io
from pathlib import Path

from aicode.tui.console import Console
from aicode.tui.render import TranscriptRenderer
from aicode.tui.theme import Palette, palette_for

from aiharness import Event


def renderer(workspace: Path | None = None) -> tuple[TranscriptRenderer, io.StringIO]:
    stream = io.StringIO()
    console = Console(stream, palette=Palette.plain(), animate=False)
    return TranscriptRenderer(console, workspace=workspace), stream


def event(event_type: str, /, **data: object) -> Event:
    return Event(type=event_type, session_id="ses-1", run_id="run-1", data=dict(data))


def test_text_deltas_stream_straight_through() -> None:
    view, stream = renderer()

    view.observe(event("model.chunk", kind="text_delta", text="Hello "))
    view.observe(event("model.chunk", kind="text_delta", text="world"))

    assert stream.getvalue() == "Hello world"


def test_thinking_is_hidden_unless_asked_for() -> None:
    view, stream = renderer()

    view.observe(event("model.chunk", kind="thinking_delta", text="hmm"))

    assert stream.getvalue() == ""


def test_a_tool_call_renders_as_a_signature_line() -> None:
    view, stream = renderer()

    view.observe(
        event(
            "tool.requested",
            tool_call_id="t1",
            tool_name="bash",
            input={"command": "pytest -q", "timeout_seconds": 30},
        )
    )

    assert "● bash(pytest -q)" in stream.getvalue()


def test_paths_are_shown_relative_to_the_workspace(tmp_path: Path) -> None:
    view, stream = renderer(tmp_path)

    view.observe(
        event(
            "tool.requested",
            tool_call_id="t1",
            tool_name="read_file",
            input={"path": str(tmp_path / "src" / "app.py")},
        )
    )

    assert "● read_file(src/app.py)" in stream.getvalue()


def test_a_result_is_summarized_not_dumped() -> None:
    view, stream = renderer()

    view.observe(
        event(
            "tool.result",
            message={
                "role": "user",
                "content": [
                    {
                        "kind": "tool_result",
                        "tool_call_id": "t1",
                        "content": "\n".join(f"line {index}" for index in range(20)),
                        "is_error": False,
                        "metadata": {"tool_name": "bash"},
                    }
                ],
            },
        )
    )

    output = stream.getvalue()
    assert "⎿ line 0" in output
    assert "… +17 lines" in output
    assert "line 19" not in output


def test_an_unstreamed_assistant_message_is_still_printed() -> None:
    """Not every provider streams text deltas; the reply must not vanish."""

    view, stream = renderer()

    view.observe(
        event(
            "assistant.message",
            message={"role": "assistant", "content": [{"kind": "text", "text": "done"}]},
        )
    )

    assert stream.getvalue() == "done\n"


def test_streamed_text_is_not_printed_twice() -> None:
    view, stream = renderer()

    view.observe(event("model.chunk", kind="text_delta", text="done"))
    view.observe(
        event(
            "assistant.message",
            message={"role": "assistant", "content": [{"kind": "text", "text": "done"}]},
        )
    )

    assert stream.getvalue() == "done\n"


def test_a_broken_event_never_escapes_the_observer() -> None:
    """An observer that raises would take the run down with the UI."""

    view, stream = renderer()

    view.observe(event("tool.result", message="not a dict"))
    view.observe(event("model.chunk"))
    view.observe(event("unknown.type", whatever=1))

    assert stream.getvalue() == ""


def test_an_approved_call_is_announced_once_not_twice() -> None:
    """The Harness re-dispatches after approval; that is not a second call."""

    view, stream = renderer()
    request = event(
        "tool.requested", tool_call_id="t1", tool_name="bash", input={"command": "echo hi"}
    )

    view.observe(request)
    view.observe(event("policy.decided", effect="ask", reason="needs approval", rule_id="r1"))
    view.observe(request)

    assert stream.getvalue().count("● bash(echo hi)") == 1
    # An ASK reason belongs on the approval prompt, not above it as well.
    assert "needs approval" not in stream.getvalue()


def test_a_multiline_result_only_gets_one_gutter_marker() -> None:
    view, stream = renderer()

    view.observe(
        event(
            "tool.result",
            message={
                "role": "user",
                "content": [
                    {"kind": "tool_result", "tool_call_id": "t1", "content": "a\nb\nc"}
                ],
            },
        )
    )

    assert stream.getvalue().count("⎿") == 1


def test_denied_policy_decisions_are_visible() -> None:
    view, stream = renderer()

    view.observe(
        event("policy.decided", effect="deny", reason="plan mode forbids writes", rule_id="r1")
    )

    assert "denied: plan mode forbids writes" in stream.getvalue()


def test_no_color_beats_force_color() -> None:
    assert palette_for(io.StringIO(), {"NO_COLOR": "1", "FORCE_COLOR": "1"}).enabled is False
    assert palette_for(io.StringIO(), {"FORCE_COLOR": "1"}).enabled is True
    assert palette_for(io.StringIO(), {}).enabled is False
