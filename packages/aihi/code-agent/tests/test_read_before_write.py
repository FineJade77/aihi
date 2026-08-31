from __future__ import annotations

import pytest
from aihi.agent import ToolContext
from aihi.code_agent.tools import (
    EditFileTool,
    ReadFileTool,
    ReadLedger,
    WriteFileTool,
)


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        cwd=str(tmp_path),
        session_id="ses_test",
        run_id="run_test",
    )


async def test_editing_a_file_this_run_never_read_is_refused(tmp_path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    ledger = ReadLedger()
    edit = EditFileTool(ledger=ledger)

    result = await edit.run(
        {"path": "a.py", "old_text": "old", "new_text": "new"}, _context(tmp_path)
    )

    assert result.is_error
    assert "read" in result.content.lower()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"


async def test_reading_first_allows_the_edit(tmp_path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    ledger = ReadLedger()
    context = _context(tmp_path)

    await ReadFileTool(ledger=ledger).run({"path": "a.py"}, context)
    result = await EditFileTool(ledger=ledger).run(
        {"path": "a.py", "old_text": "old", "new_text": "new"}, context
    )

    assert not result.is_error
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"


async def test_a_read_in_another_run_does_not_count(tmp_path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    ledger = ReadLedger()
    first = _context(tmp_path)
    await ReadFileTool(ledger=ledger).run({"path": "a.py"}, first)

    second = ToolContext(
        cwd=str(tmp_path),
        session_id="ses_test",
        run_id="run_other",
    )
    result = await EditFileTool(ledger=ledger).run(
        {"path": "a.py", "old_text": "old", "new_text": "new"}, second
    )

    assert result.is_error


async def test_writing_a_new_file_needs_no_prior_read(tmp_path) -> None:
    ledger = ReadLedger()
    result = await WriteFileTool(ledger=ledger).run(
        {"path": "fresh.py", "content": "print()\n"}, _context(tmp_path)
    )

    assert not result.is_error
    assert (tmp_path / "fresh.py").read_text(encoding="utf-8") == "print()\n"


async def test_overwriting_an_existing_file_needs_a_prior_read(tmp_path) -> None:
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    ledger = ReadLedger()

    result = await WriteFileTool(ledger=ledger).run(
        {"path": "a.py", "content": "new\n"}, _context(tmp_path)
    )

    assert result.is_error
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"


async def test_without_a_ledger_the_guard_is_absent(tmp_path) -> None:
    # Existing embedders construct these tools with no arguments at all.
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "a.py", "old_text": "old", "new_text": "new"}, _context(tmp_path)
    )
    assert not result.is_error


def test_the_ledger_bounds_how_many_runs_it_remembers() -> None:
    ledger = ReadLedger(max_runs=2)
    for index in range(3):
        ledger.record(f"run_{index}", "/tmp/a.py")
    assert not ledger.has_read("run_0", "/tmp/a.py")
    assert ledger.has_read("run_2", "/tmp/a.py")


@pytest.mark.parametrize("tool", [EditFileTool, WriteFileTool, ReadFileTool])
def test_tools_still_construct_without_a_ledger(tool) -> None:
    assert tool().spec.name
