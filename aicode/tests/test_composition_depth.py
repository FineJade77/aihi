"""The system prompt, project rules, artifacts and telemetry actually compose."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aicode.app import build_runtime
from aicode.config import AICodeConfig
from aicode.context import ProjectRulesContributor, find_rules_file
from aicode.prompt import SYSTEM_PROMPT
from typer.testing import CliRunner

from aiharness import FakeProvider, InMemoryEventStore
from aiharness.models.providers.fake import FakeStep

runner = CliRunner()

RULES = "# House rules\n\nAlways run `make check` before claiming a change works.\n"


def workspace(tmp_path: Path, *, rules: str | None = RULES) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    if rules is not None:
        (root / "AGENTS.md").write_text(rules, encoding="utf-8")
    return root


def test_rules_file_is_rendered_with_precedence(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    sections = ProjectRulesContributor(root).sections(object())

    assert len(sections) == 1
    assert sections[0].source == "project_rules"
    assert "From AGENTS.md" in sections[0].body
    assert "make check" in sections[0].body


def test_no_rules_file_contributes_nothing(tmp_path: Path) -> None:
    assert ProjectRulesContributor(workspace(tmp_path, rules=None)).sections(object()) == ()


def test_oversized_rules_are_truncated(tmp_path: Path) -> None:
    root = workspace(tmp_path, rules="x" * 5_000)

    sections = ProjectRulesContributor(root, max_bytes=1_000).sections(object())

    assert "[Project rules truncated.]" in sections[0].body
    assert len(sections[0].body) < 2_000


def test_a_rules_symlink_out_of_the_workspace_is_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secrets from elsewhere", encoding="utf-8")
    root = workspace(tmp_path, rules=None)
    try:
        (root / "AGENTS.md").symlink_to(outside)
    except OSError:  # pragma: no cover - platforms without symlink permission
        pytest.skip("symlinks unavailable")

    assert find_rules_file(root) is None
    assert ProjectRulesContributor(root).sections(object()) == ()


def test_runtime_composes_artifact_store_and_optional_telemetry(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    telemetry_file = tmp_path / "telemetry.jsonl"

    plain = build_runtime(AICodeConfig(workspace=root, unsafe_host=True))
    assert plain.coordinator.artifact_store is not None
    assert plain.telemetry is None
    assert plain.system_prompt == SYSTEM_PROMPT

    observed = build_runtime(
        AICodeConfig(workspace=root, unsafe_host=True, telemetry_path=telemetry_file)
    )
    assert observed.coordinator.telemetry is not None


@pytest.mark.asyncio
async def test_a_real_run_sends_prompt_plus_rules_and_records_telemetry(tmp_path: Path) -> None:
    import asyncio

    from aiharness import Message, Session

    root = workspace(tmp_path)
    telemetry_file = tmp_path / "telemetry.jsonl"
    config = AICodeConfig(workspace=root, unsafe_host=True, telemetry_path=telemetry_file)
    runtime = build_runtime(config)
    provider = FakeProvider([FakeStep(text="done")])
    runtime.coordinator.provider = provider
    session = Session.create(InMemoryEventStore(), cwd=root, provider="fake", model="fake-model")

    await runtime.coordinator.run(
        session,
        model="fake-model",
        user_message=Message.text("user", "hi"),
        system_prompt=runtime.system_prompt,
    )
    await asyncio.sleep(0)

    sent = provider.requests[0].system_prompt
    assert "You are aicode" in sent
    assert "Project conventions" in sent
    assert "make check" in sent
    lines = telemetry_file.read_text(encoding="utf-8").splitlines()
    assert lines
    kinds = {json.loads(line)["name"] for line in lines}
    assert "run.completed" in kinds
    assert "model.chunk" not in kinds


def test_cli_run_uses_the_product_prompt(tmp_path: Path) -> None:
    from aicode.cli import app

    from aicode import app as app_module

    root = workspace(tmp_path)
    seen: list[str] = []

    class Recording(FakeProvider):
        def stream(self, request):  # type: ignore[no-untyped-def]
            seen.append(request.system_prompt)
            return super().stream(request)

    original = app_module.build_provider
    app_module.build_provider = lambda config: Recording([FakeStep(text="ok")])
    try:
        result = runner.invoke(
            app,
            [
                "run",
                "hello",
                "--db",
                str(tmp_path / "events.db"),
                "--workspace",
                str(root),
                "--unsafe-host",
            ],
            env={**os.environ},
        )
    finally:
        app_module.build_provider = original

    assert result.exit_code == 0, result.output
    assert "You are aicode" in seen[0]
    assert "make check" in seen[0]
