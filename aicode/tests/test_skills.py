"""The project skill index reaching a real aicode run."""

from __future__ import annotations

import json
from pathlib import Path

from aicode.app import build_extensions, build_runtime
from aicode.cli import app
from aicode.config import AICodeConfig
from typer.testing import CliRunner

runner = CliRunner()

SKILL = """---
name: repo-conventions
description: How this repository names branches and commits
version: 0.3.0
---

# Conventions

Body text that must stay out of the model context.
"""


def workspace_with_skill(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    root = workspace / ".aicode" / "skills" / "repo-conventions"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return workspace


def test_no_skills_directory_composes_no_extensions(tmp_path: Path) -> None:
    workspace = tmp_path / "empty"
    workspace.mkdir()

    extensions = build_extensions(AICodeConfig(workspace=workspace))

    assert extensions.empty is True


def test_project_skills_are_composed_into_the_runtime(tmp_path: Path) -> None:
    workspace = workspace_with_skill(tmp_path)

    runtime = build_runtime(AICodeConfig(workspace=workspace, unsafe_host=True))

    assert len(runtime.extensions.context_contributors) == 1
    sections = runtime.extensions.context_contributors[0].sections(object())
    assert "repo-conventions@0.3.0" in sections[0].body
    assert "Body text" not in sections[0].body


def test_cli_run_sends_the_skill_index_and_not_the_body(tmp_path: Path) -> None:
    workspace = workspace_with_skill(tmp_path)
    database = tmp_path / "events.db"

    result = runner.invoke(
        app,
        [
            "run",
            "follow the conventions",
            "--db",
            str(database),
            "--workspace",
            str(workspace),
            "--unsafe-host",
        ],
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert any(event["type"] == "run.completed" for event in events)
    # The index is metadata; the body never leaves the trust flow.
    assert "Body text" not in result.output
