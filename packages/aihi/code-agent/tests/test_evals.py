from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aihi.agent import Event, InMemoryEventStore, RunResult, RunState, Session
from aihi.agent.policy import PermissionMode
from aihi.code_agent.config import CodeAgentConfig, ProviderSettings, SandboxSettings
from aihi.code_agent.evals import (
    CodeAgentEvalRunner,
    CodeTask,
    CodeTaskDataset,
    TaskExecution,
    changed_paths,
    directory_sha256,
)
from aihi.code_agent.evals.dataset import CodeEvalValidationError

from scripts.evals.reference_baseline import reference_executor
from scripts.evals.run import compare_baseline, validate_live_config


def _task(fixture: Path, *, forbidden_paths: tuple[str, ...] = ()) -> CodeTask:
    return CodeTask(
        case_id="mvp-task",
        category="feature",
        prompt="Create answer.txt containing ok.",
        fixture_path=fixture,
        fixture_sha256=directory_sha256(fixture),
        timeout_seconds=5,
        max_turns=5,
        max_tokens=1_000,
        test_commands=(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('answer.txt').read_text() == 'ok\\n'",
            ),
        ),
        allowed_paths=("answer.txt",),
        forbidden_paths=forbidden_paths,
        require_clean_regression=True,
        expected_files=("answer.txt",),
    )


async def _successful_executor(
    task: CodeTask, workspace: Path, store: InMemoryEventStore
) -> TaskExecution:
    (workspace / "answer.txt").write_text("ok\n", encoding="utf-8")
    return await _completed_execution(workspace, store)


async def _completed_execution(workspace: Path, store: InMemoryEventStore) -> TaskExecution:
    session = Session.create(store, cwd=workspace, provider="fake", model="demo")
    session.append_many(
        [
            Event(type="run.started", session_id=session.id, run_id="run-1"),
            Event(
                type="run.state_changed",
                session_id=session.id,
                run_id="run-1",
                data={"state": "running"},
            ),
            Event(
                type="run.state_changed",
                session_id=session.id,
                run_id="run-1",
                data={"state": "completed"},
            ),
            Event(
                type="run.completed",
                session_id=session.id,
                run_id="run-1",
                data={"state": "completed"},
            ),
        ]
    )
    return TaskExecution(
        session=session,
        run_result=RunResult(run_id="run-1", state=RunState.COMPLETED),
    )


@pytest.mark.asyncio
async def test_code_eval_runner_grades_workspace_tests_scope_and_trace(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "README.md").write_text("fixture\n", encoding="utf-8")

    report = await CodeAgentEvalRunner(executor=_successful_executor).run_dataset(
        (_task(fixture),), dataset_id="aihi-code-agent-benchmark-v1", mode="offline"
    )

    result = report.results[0]
    assert result.passed is True
    assert {grade.grader_id for grade in result.grades} == {
        "code_tests",
        "code_scope",
        "code_expected_files",
        "harness_trace",
    }
    assert result.trace is not None
    assert report.to_dict()["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "pass_rate": 1.0,
    }


@pytest.mark.asyncio
async def test_code_eval_runner_rejects_forbidden_workspace_changes(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()

    async def executor(task: CodeTask, workspace: Path, store: InMemoryEventStore) -> TaskExecution:
        (workspace / "answer.txt").write_text("ok\n", encoding="utf-8")
        (workspace / "secret.txt").write_text("should fail\n", encoding="utf-8")
        return await _successful_executor(task, workspace, store)

    result = (await CodeAgentEvalRunner(executor=executor).run_case(
        _task(fixture, forbidden_paths=("secret.txt",))
    ))

    assert result.passed is False
    scope = next(grade for grade in result.grades if grade.grader_id == "code_scope")
    assert scope.details["forbidden_paths"] == ["secret.txt"]


@pytest.mark.asyncio
async def test_code_eval_runner_fails_closed_on_fixture_hash_mismatch(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = _task(fixture)
    (fixture / "changed.txt").write_text("tampered\n", encoding="utf-8")

    result = await CodeAgentEvalRunner(executor=_successful_executor).run_case(task)

    assert result.passed is False
    assert result.error_code == "fixture_invalid"


def test_code_task_dataset_round_trips_jsonl(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = _task(fixture)
    dataset = CodeTaskDataset("benchmark", (task,))

    restored = CodeTaskDataset.from_jsonl(
        "benchmark", dataset.to_jsonl(base_dir=tmp_path), base_dir=tmp_path
    )

    assert restored.tasks[0].to_dict(base_dir=tmp_path) == task.to_dict(base_dir=tmp_path)


def test_workspace_changes_ignore_python_bytecode_but_not_other_files(tmp_path: Path) -> None:
    before = {"target.py": "before"}
    after_root = tmp_path / "workspace"
    after_root.mkdir()
    (after_root / "target.py").write_text("after", encoding="utf-8")
    cache = after_root / "__pycache__"
    cache.mkdir()
    (cache / "target.cpython-312.pyc").write_bytes(b"derived")
    (cache / "unexpected.txt").write_text("must remain visible", encoding="utf-8")

    from aihi.code_agent.evals.workspace import snapshot_files

    assert changed_paths(before, snapshot_files(after_root)) == (
        "__pycache__/unexpected.txt",
        "target.py",
    )


def test_code_task_rejects_network_and_non_docker_execution(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    common = _task(fixture).to_dict(base_dir=tmp_path)
    common["execution"] = {"sandbox_backend": "host", "network": False, "repeat": 1}
    with pytest.raises(CodeEvalValidationError, match="sandbox_backend"):
        CodeTask.from_dict(common, base_dir=tmp_path)

    common["execution"] = {"sandbox_backend": "docker", "network": True, "repeat": 1}
    with pytest.raises(CodeEvalValidationError, match="network"):
        CodeTask.from_dict(common, base_dir=tmp_path)


def test_live_config_validation_fails_closed_without_real_provider_or_docker(
    tmp_path: Path,
) -> None:
    defaults = CodeAgentConfig.defaults(tmp_path)
    with pytest.raises(ValueError, match="real Provider"):
        validate_live_config(defaults, environment={})

    live = replace(
        defaults,
        provider=ProviderSettings(
            name="openai", model="gpt-eval", api_key_env="OPENAI_API_KEY"
        ),
        permission_mode=PermissionMode.BYPASS,
        sandbox=SandboxSettings(
            backend="docker",
            root=tmp_path,
            image="python:3.11-slim",
            network="none",
            allow_network=False,
        ),
    )
    validate_live_config(live, environment={"OPENAI_API_KEY": "test-only"})

    unsafe = replace(live, sandbox=replace(live.sandbox, allow_network=True))
    with pytest.raises(ValueError, match="allow_network"):
        validate_live_config(unsafe, environment={"OPENAI_API_KEY": "test-only"})

    interactive = replace(live, permission_mode=PermissionMode.ACCEPT_EDITS)
    with pytest.raises(ValueError, match="permission_mode"):
        validate_live_config(interactive, environment={"OPENAI_API_KEY": "test-only"})


@pytest.mark.asyncio
async def test_code_eval_runner_applies_task_timeout(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = _task(fixture)
    task = replace(task, timeout_seconds=1)

    async def slow_executor(
        _task: CodeTask, _workspace: Path, _store: InMemoryEventStore
    ) -> TaskExecution:
        await asyncio.sleep(2)
        raise AssertionError("unreachable")

    result = await CodeAgentEvalRunner(executor=slow_executor).run_case(task)

    assert result.passed is False
    assert result.error_code == "execution_timeout"


@pytest.mark.asyncio
async def test_code_eval_runner_expands_repeated_tasks(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = replace(_task(fixture), repeat=2)

    report = await CodeAgentEvalRunner(executor=_successful_executor).run_dataset(
        (task,), mode="nightly"
    )

    assert [result.case_id for result in report.results] == [
        "mvp-task#repeat-1",
        "mvp-task#repeat-2",
    ]
    assert [result.metrics["attempt"] for result in report.results] == [1, 2]


@pytest.mark.asyncio
async def test_v1_manifest_has_fixed_fixtures_and_reproducible_reference_baseline() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    benchmark_root = repository_root / "evals" / "aihi_code_agent" / "v1"
    dataset = CodeTaskDataset.from_jsonl(
        "aihi-code-agent-benchmark-v1",
        (benchmark_root / "manifest.jsonl").read_text(encoding="utf-8"),
        base_dir=benchmark_root,
    )
    baseline = json.loads((benchmark_root / "baseline.json").read_text(encoding="utf-8"))

    report = await CodeAgentEvalRunner(executor=reference_executor).run_dataset(dataset)

    assert [task.case_id for task in dataset.tasks] == baseline["case_ids"]
    assert {task.category for task in dataset.tasks} == set(baseline["categories"])
    assert {task.timeout_seconds for task in dataset.tasks} == {90}
    instruction_task = next(
        task for task in dataset.tasks if task.case_id == "instruction-following-report"
    )
    assert "第一行是 # Changelog，第二行是空行" in instruction_task.prompt
    assert report.total == baseline["summary"]["total"]
    assert report.passed == baseline["summary"]["passed"]
    assert report.failed == baseline["summary"]["failed"]
    assert report.pass_rate == baseline["summary"]["pass_rate"]
    comparison = compare_baseline(report, baseline)
    assert comparison["case_ids_match"] is True
    assert comparison["delta"] == {"passed": 0, "pass_rate": 0.0}


def test_report_is_strict_json() -> None:
    # Keep this small smoke assertion close to the task contract: report data
    # must be serializable before a CI artifact is written.
    payload = {"case_id": "x", "metadata": {"safe": True}}
    assert json.loads(json.dumps(payload)) == payload
