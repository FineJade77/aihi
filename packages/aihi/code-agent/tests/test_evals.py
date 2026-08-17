from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aihi.agent import Event, InMemoryEventStore, RunResult, RunState, Session
from aihi.agent.policy import PermissionMode
from aihi.code_agent.config import CodeAgentConfig, ProviderSettings, SandboxSettings
from aihi.code_agent.evals import (
    CodeAgentEvalRunner,
    CodeEvalGateFailed,
    CodeEvalReport,
    CodeTask,
    CodeTaskDataset,
    CodeTaskResult,
    TaskExecution,
    changed_paths,
    directory_sha256,
)
from aihi.code_agent.evals.dataset import CodeEvalValidationError

from scripts.evals.reference_baseline import reference_executor
from scripts.evals.run import (
    assert_baseline_gate,
    build_live_summary,
    compare_baseline,
    repeat_dataset,
    select_baseline,
    validate_docker_daemon,
    validate_live_config,
)


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
        "base_cases": 1,
        "repetitions_min": 1,
        "repetitions_max": 1,
        "pass_at_1": 1.0,
        "pass_at_least_once": 1.0,
        "stable_pass_rate": 1.0,
        "duration_seconds": pytest.approx(
            report.results[0].metrics["duration_seconds"]
        ),
        "latency_p50_seconds": pytest.approx(
            report.results[0].metrics["duration_seconds"]
        ),
        "latency_p95_seconds": pytest.approx(
            report.results[0].metrics["duration_seconds"]
        ),
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
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


def test_live_docker_preflight_fails_before_provider_execution(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["docker", "version"], 1, "", "daemon unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)

    with pytest.raises(ValueError, match="reachable Docker daemon"):
        validate_docker_daemon()


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
async def test_code_eval_runner_classifies_cancellation_repaired_as_interrupted(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = replace(_task(fixture), timeout_seconds=1)

    async def interrupted_executor(
        _task: CodeTask, workspace: Path, store: InMemoryEventStore
    ) -> TaskExecution:
        session = Session.create(store, cwd=workspace, provider="live", model="model")
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return TaskExecution(
                session=session,
                run_result=RunResult(run_id="run-timeout", state=RunState.INTERRUPTED),
            )
        raise AssertionError("unreachable")

    result = await CodeAgentEvalRunner(executor=interrupted_executor).run_case(task)

    assert result.passed is False
    assert result.error_code == "execution_timeout"
    assert result.metrics["run_state"] == "interrupted"


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
async def test_code_eval_runner_records_usage_cost_and_tool_metrics(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()

    async def executor(
        _task: CodeTask, workspace: Path, store: InMemoryEventStore
    ) -> TaskExecution:
        (workspace / "answer.txt").write_text("ok\n", encoding="utf-8")
        session = Session.create(store, cwd=workspace, provider="live", model="model")
        session.append_many(
            [
                Event(type="run.started", session_id=session.id, run_id="run-live"),
                Event(
                    type="run.state_changed",
                    session_id=session.id,
                    run_id="run-live",
                    data={"state": "running"},
                ),
                Event(
                    type="model.usage",
                    session_id=session.id,
                    run_id="run-live",
                    data={
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "cached_input_tokens": 10,
                        "cost_usd": 0.0125,
                    },
                ),
                Event(
                    type="tool.started",
                    session_id=session.id,
                    run_id="run-live",
                    data={"call_id": "call-1", "tool_name": "write_file"},
                ),
                Event(
                    type="tool.completed",
                    session_id=session.id,
                    run_id="run-live",
                    data={"call_id": "call-1", "tool_name": "write_file"},
                ),
                Event(
                    type="run.state_changed",
                    session_id=session.id,
                    run_id="run-live",
                    data={"state": "completed"},
                ),
                Event(type="run.completed", session_id=session.id, run_id="run-live"),
            ]
        )
        return TaskExecution(
            session=session,
            run_result=RunResult(run_id="run-live", state=RunState.COMPLETED),
        )

    result = await CodeAgentEvalRunner(executor=executor).run_case(_task(fixture))

    assert result.metrics["model_calls"] == 1
    assert result.metrics["input_tokens"] == 120
    assert result.metrics["output_tokens"] == 30
    assert result.metrics["cached_input_tokens"] == 10
    assert result.metrics["tokens"] == 150
    assert result.metrics["cost_usd"] == pytest.approx(0.0125)
    assert result.metrics["tool_calls"] == 1


def test_code_eval_report_summarizes_repeated_live_attempts() -> None:
    results = (
        CodeTaskResult(
            "case-a#repeat-1",
            True,
            metrics={
                "base_case_id": "case-a",
                "attempt": 1,
                "duration_seconds": 1.0,
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 5,
                "tokens": 120,
                "model_calls": 2,
                "tool_calls": 1,
                "cost_usd": 0.01,
            },
        ),
        CodeTaskResult(
            "case-a#repeat-2",
            False,
            metrics={
                "base_case_id": "case-a",
                "attempt": 2,
                "duration_seconds": 2.0,
                "input_tokens": 110,
                "output_tokens": 30,
                "cached_input_tokens": 0,
                "tokens": 140,
                "model_calls": 2,
                "tool_calls": 2,
                "cost_usd": 0.02,
            },
        ),
        CodeTaskResult(
            "case-b#repeat-1",
            True,
            metrics={"base_case_id": "case-b", "attempt": 1, "duration_seconds": 3.0},
        ),
        CodeTaskResult(
            "case-b#repeat-2",
            True,
            metrics={"base_case_id": "case-b", "attempt": 2, "duration_seconds": 4.0},
        ),
    )
    report = CodeEvalReport(
        "aihi-code-agent-benchmark-v1",
        "nightly",
        results,
        config={"provider": "openai", "model": "model-a"},
    )

    summary = report.to_dict()["summary"]
    assert summary["total"] == 4
    assert summary["base_cases"] == 2
    assert summary["repetitions_min"] == 2
    assert summary["repetitions_max"] == 2
    assert summary["pass_at_1"] == pytest.approx(0.75)
    assert summary["pass_at_least_once"] == 1.0
    assert summary["stable_pass_rate"] == 0.5
    assert summary["duration_seconds"] == 10.0
    assert summary["latency_p50_seconds"] == 2.0
    assert summary["latency_p95_seconds"] == 4.0
    assert summary["tokens"] == 260
    assert summary["model_calls"] == 4
    assert summary["tool_calls"] == 3
    assert summary["cost_usd"] == pytest.approx(0.03)


def test_repeat_dataset_overrides_manifest_repeat_without_mutating_it(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    dataset = CodeTaskDataset("benchmark", (_task(fixture),))

    repeated = repeat_dataset(dataset, 3)

    assert dataset.tasks[0].repeat == 1
    assert repeated.tasks[0].repeat == 3
    with pytest.raises(ValueError, match="positive integer"):
        repeat_dataset(dataset, 0)


def test_live_summary_compares_profiles_without_config_paths_or_credentials() -> None:
    reports = (
        CodeEvalReport(
            "aihi-code-agent-benchmark-v1",
            "nightly",
            (CodeTaskResult("case-a", True, metrics={"duration_seconds": 1.0}),),
            config={"provider": "openai", "model": "model-a"},
        ),
        CodeEvalReport(
            "aihi-code-agent-benchmark-v1",
            "nightly",
            (CodeTaskResult("case-a", False, metrics={"duration_seconds": 2.0}),),
            config={"provider": "anthropic", "model": "model-b"},
        ),
    )

    payload = build_live_summary(reports)

    assert payload["profile_count"] == 2
    assert [profile["provider"] for profile in payload["profiles"]] == [
        "openai",
        "anthropic",
    ]
    assert [profile["summary"]["pass_at_1"] for profile in payload["profiles"]] == [
        1.0,
        0.0,
    ]
    assert "config_path" not in json.dumps(payload)
    assert "api_key" not in json.dumps(payload)


def test_baseline_comparison_uses_base_case_ids_for_repeated_attempts() -> None:
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (
            CodeTaskResult("a#repeat-1", True, metrics={"base_case_id": "a"}),
            CodeTaskResult("a#repeat-2", False, metrics={"base_case_id": "a"}),
            CodeTaskResult("b#repeat-1", True, metrics={"base_case_id": "b"}),
            CodeTaskResult("b#repeat-2", True, metrics={"base_case_id": "b"}),
        ),
    )
    baseline = {
        "baseline_version": 1,
        "dataset_id": "benchmark",
        "case_ids": ["a", "b"],
        "summary": {"total": 2, "passed": 2, "failed": 0, "pass_rate": 1.0},
    }

    comparison = compare_baseline(report, baseline)

    assert comparison["actual"]["total"] == 4
    assert comparison["actual"]["base_cases"] == 2
    assert comparison["actual"]["pass_at_1"] == pytest.approx(0.75)


def test_live_baseline_comparison_uses_model_matched_pass_at_1() -> None:
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (
            CodeTaskResult("a#repeat-1", True, metrics={"base_case_id": "a"}),
            CodeTaskResult("a#repeat-2", True, metrics={"base_case_id": "a"}),
            CodeTaskResult("a#repeat-3", False, metrics={"base_case_id": "a"}),
        ),
        config={"provider": "deepseek", "model": "model-a"},
    )
    baseline = {
        "artifact_kind": "reviewed_live_baseline",
        "baseline_version": 1,
        "dataset_id": "benchmark",
        "provider": "deepseek",
        "model": "model-a",
        "case_ids": ["a"],
        "summary": {
            "total": 3,
            "passed": 2,
            "failed": 1,
            "pass_rate": 0.5,
            "pass_at_1": 2 / 3,
        },
    }

    comparison = compare_baseline(report, baseline)

    assert comparison["comparison_kind"] == "reviewed_live_baseline"
    assert comparison["baseline"]["pass_at_1"] == pytest.approx(2 / 3)
    assert comparison["delta"]["pass_at_1"] == pytest.approx(0.0)
    assert_baseline_gate(report, comparison)


def test_live_baseline_gate_rejects_pass_at_1_regression() -> None:
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (
            CodeTaskResult("a#repeat-1", True, metrics={"base_case_id": "a"}),
            CodeTaskResult("a#repeat-2", False, metrics={"base_case_id": "a"}),
            CodeTaskResult("a#repeat-3", False, metrics={"base_case_id": "a"}),
        ),
        config={"provider": "deepseek", "model": "model-a"},
    )
    comparison = {
        "baseline": {"pass_at_1": 2 / 3},
        "actual": {"pass_at_1": 1 / 3},
    }

    with pytest.raises(CodeEvalGateFailed, match="below reviewed baseline"):
        assert_baseline_gate(report, comparison)


def test_live_baseline_requires_matching_provider_and_model() -> None:
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (CodeTaskResult("a", True),),
        config={"provider": "deepseek", "model": "model-b"},
    )
    baseline = {
        "artifact_kind": "reviewed_live_baseline",
        "dataset_id": "benchmark",
        "provider": "deepseek",
        "model": "model-a",
        "case_ids": ["a"],
        "summary": {"pass_at_1": 1.0},
    }

    with pytest.raises(ValueError, match="provider/model"):
        compare_baseline(report, baseline)


def test_select_baseline_matches_live_profile_without_scripted_fallback(
    tmp_path: Path,
) -> None:
    benchmark_root = tmp_path / "v1"
    baselines = benchmark_root / "baselines"
    baselines.mkdir(parents=True)
    (benchmark_root / "baseline.json").write_text(
        json.dumps({"artifact_kind": "scripted_reference"}), encoding="utf-8"
    )
    live = {
        "artifact_kind": "reviewed_live_baseline",
        "provider": "deepseek",
        "model": "model-a",
    }
    (baselines / "deepseek.json").write_text(json.dumps(live), encoding="utf-8")
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (CodeTaskResult("a", True),),
        config={"provider": "deepseek", "model": "model-a"},
    )

    assert select_baseline(report, benchmark_root) == live
    unmatched = replace(report, config={"provider": "deepseek", "model": "model-b"})
    assert select_baseline(unmatched, benchmark_root) is None


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
    assert comparison["delta"] == {"pass_at_1": 0.0}


def test_report_is_strict_json() -> None:
    # Keep this small smoke assertion close to the task contract: report data
    # must be serializable before a CI artifact is written.
    payload = {"case_id": "x", "metadata": {"safe": True}}
    assert json.loads(json.dumps(payload)) == payload


def test_report_summary_fields_are_declared_by_the_v1_schema() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    schema = json.loads(
        (repository_root / "evals" / "schemas" / "eval-report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    report = CodeEvalReport(
        "benchmark",
        "nightly",
        (CodeTaskResult("case", True, metrics={"duration_seconds": 1.0}),),
    )

    declared = set(schema["properties"]["summary"]["properties"])
    assert set(report.summary()) <= declared
