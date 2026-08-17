"""Isolated Coding Agent task execution and deterministic post-run grading."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from aihi.agent import EventStore, InMemoryEventStore, Session
from aihi.agent.evals import TraceBundle
from aihi.agent.observability import Redactor
from aihi.agent.runtime import RunResult
from aihi.code_agent.config import CodeAgentConfig, CodeAgentConfigError
from aihi.code_agent.evals.dataset import CodeEvalValidationError, CodeTask, CodeTaskDataset
from aihi.code_agent.evals.graders import (
    CommandOutcome,
    grade_commands,
    grade_expected_files,
    grade_harness_trace,
    grade_scope,
)
from aihi.code_agent.evals.report import CodeEvalReport, CodeTaskResult
from aihi.code_agent.evals.workspace import WorkspaceManager, changed_paths
from aihi.code_agent.prompts import build_system_prompt
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.models import Message

_MAX_COMMAND_OUTPUT = 4_096


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """The durable Session produced by an injected or configured executor."""

    session: Session
    run_result: RunResult | None = None


class TaskExecutor(Protocol):
    async def __call__(
        self, task: CodeTask, workspace: Path, store: EventStore
    ) -> TaskExecution: ...


CommandExecutor = Callable[[tuple[str, ...], Path, float], Awaitable[CommandOutcome]]


class CodeAgentEvalRunner:
    """Run Code Tasks serially in disposable fixture workspaces.

    The executor is injectable so PR tests can use a deterministic scripted
    executor.  Supplying ``config`` enables the default path, which assembles
    the real :class:`CodeAgentRuntime` against the task workspace.
    """

    def __init__(
        self,
        *,
        executor: TaskExecutor | None = None,
        config: CodeAgentConfig | None = None,
        workspace_manager: WorkspaceManager | None = None,
        command_executor: CommandExecutor | None = None,
    ) -> None:
        if executor is None and config is None:
            raise ValueError("CodeAgentEvalRunner requires executor or config")
        self.executor = executor
        self.config = config
        self.workspace_manager = workspace_manager or WorkspaceManager()
        self.command_executor = command_executor or _run_command

    async def run_case(self, task: CodeTask) -> CodeTaskResult:
        started = time.perf_counter()
        try:
            lease = self.workspace_manager.prepare(task)
        except CodeEvalValidationError as exc:
            return CodeTaskResult(
                case_id=task.case_id,
                passed=False,
                error_code="fixture_invalid",
                metrics={"error": str(exc)},
            )

        with lease:
            execution: TaskExecution | None = None
            execution_error: str | None = None
            error_text: str | None = None
            try:
                executor = self.executor or self._runtime_executor
                execution = await asyncio.wait_for(
                    executor(task, lease.root, InMemoryEventStore()),
                    timeout=task.timeout_seconds,
                )
                if not isinstance(execution, TaskExecution):
                    raise TypeError("task executor must return TaskExecution")
            except TimeoutError:
                execution_error = "execution_timeout"
            except Exception as exc:
                execution_error = "execution_error"
                execution = None
                error_text = str(exc)

            after = lease.snapshot_after()
            modified = changed_paths(lease.before, after)
            grades = [
                grade_scope(
                    modified,
                    allowed_paths=task.allowed_paths,
                    forbidden_paths=task.forbidden_paths,
                ),
                grade_expected_files(lease.root, task.expected_files),
            ]
            trace: TraceBundle | None = None
            trace_error: str | None = None
            command_outcomes: tuple[CommandOutcome, ...] = ()
            if execution is not None:
                command_outcomes = tuple(
                    [
                        await self.command_executor(command, lease.root, task.timeout_seconds)
                        for command in task.test_commands
                    ]
                )
                grades.insert(
                    0,
                    grade_commands(
                        command_outcomes,
                        require_clean_regression=task.require_clean_regression,
                    ),
                )
                try:
                    trace = TraceBundle.from_events(execution.session.events)
                except Exception as exc:  # Trace export is a grading failure, not a crash.
                    grades.append(
                        grade_harness_trace(None)
                    )
                    trace_error = type(exc).__name__
                else:
                    grades.append(grade_harness_trace(trace))
            else:
                grades.append(
                    grade_commands((), require_clean_regression=task.require_clean_regression)
                )
                grades.append(grade_harness_trace(None))

            metrics: dict[str, object] = {
                "duration_seconds": time.perf_counter() - started,
                "changed_files": len(modified),
                "changed_paths": list(modified),
                "command_count": len(command_outcomes),
            }
            if error_text is not None:
                metrics["error"] = error_text
            if trace_error is not None:
                metrics["trace_error"] = trace_error
            if execution is not None and execution.run_result is not None:
                metrics["run_id"] = execution.run_result.run_id
                metrics["run_state"] = execution.run_result.state.value
                metrics["pending_tool_calls"] = len(execution.run_result.pending_tool_call_ids)
            return CodeTaskResult(
                case_id=task.case_id,
                passed=execution_error is None and all(grade.passed for grade in grades),
                grades=tuple(grades),
                metrics=metrics,
                trace=trace,
                error_code=execution_error,
            )

    async def run_dataset(
        self,
        dataset: CodeTaskDataset | Iterable[CodeTask],
        *,
        dataset_id: str | None = None,
        mode: str = "offline",
    ) -> CodeEvalReport:
        if isinstance(dataset, CodeTaskDataset):
            tasks = dataset.tasks
            resolved_id = dataset.dataset_id
        else:
            tasks = tuple(dataset)
            resolved_id = dataset_id or "aihi-code-agent-benchmark-v1"
        results_list: list[CodeTaskResult] = []
        for task in tasks:
            for attempt in range(task.repeat):
                result = await self.run_case(task)
                if task.repeat > 1:
                    result = replace(
                        result,
                        case_id=f"{task.case_id}#repeat-{attempt + 1}",
                        metrics={
                            **result.metrics,
                            "attempt": attempt + 1,
                            "base_case_id": task.case_id,
                        },
                    )
                results_list.append(result)
        results = tuple(results_list)
        return CodeEvalReport(
            dataset_id=resolved_id,
            mode=mode,
            results=results,
            config=self._report_config(),
        )

    async def _runtime_executor(
        self, task: CodeTask, workspace: Path, store: EventStore
    ) -> TaskExecution:
        if self.config is None:  # pragma: no cover - constructor enforces this
            raise CodeAgentConfigError("runtime executor requires a CodeAgentConfig")
        if self.config.sandbox.backend != "docker":
            raise CodeAgentConfigError("benchmark execution requires the Docker sandbox")
        if self.config.sandbox.allow_network:
            raise CodeAgentConfigError("benchmark execution requires network access to be disabled")
        if self.config.permission_mode.value != "bypass":
            raise CodeAgentConfigError(
                "benchmark execution requires permission_mode=bypass for non-interactive "
                "process execution"
            )
        scoped = replace(
            self.config,
            base_dir=workspace,
            artifact_path=None,
            audit_path=None,
            sandbox=replace(self.config.sandbox, root=workspace, allow_network=False),
        )
        session = Session.create(
            store,
            cwd=workspace,
            provider=scoped.provider.name,
            model=scoped.provider.model,
        )
        runtime = await CodeAgentRuntime.create(scoped, store=store)
        try:
            result = await runtime.runtime.coordinator.run(
                session,
                model=scoped.provider.model,
                user_message=Message.text("user", task.prompt),
                permission_mode=scoped.permission_mode,
                require_capability_lease=scoped.require_capability_lease,
                system_prompt=build_system_prompt(scoped, workspace=workspace),
                max_turns=task.max_turns,
                max_output_tokens=task.max_tokens,
            )
        finally:
            await runtime.close()
        return TaskExecution(session=session, run_result=result)

    def _report_config(self) -> dict[str, object]:
        if self.config is None:
            return {}
        return {
            "provider": self.config.provider.name,
            "model": self.config.provider.model,
        }


async def _run_command(argv: tuple[str, ...], cwd: Path, timeout: float) -> CommandOutcome:
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandOutcome(
            argv=argv,
            exit_code=None,
            stderr=f"{type(exc).__name__}: command could not be started",
            duration_seconds=time.perf_counter() - started,
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        _terminate_process(process)
        await process.wait()
        return CommandOutcome(
            argv=argv,
            exit_code=None,
            stderr="command timed out",
            timed_out=True,
            duration_seconds=time.perf_counter() - started,
        )
    redactor = Redactor(max_string=_MAX_COMMAND_OUTPUT)
    safe_stdout = redactor.redact(stdout.decode("utf-8", errors="replace"))
    safe_stderr = redactor.redact(stderr.decode("utf-8", errors="replace"))
    return CommandOutcome(
        argv=argv,
        exit_code=process.returncode,
        stdout=str(safe_stdout),
        stderr=str(safe_stderr),
        duration_seconds=time.perf_counter() - started,
    )


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None or process.pid is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


__all__ = ["CodeAgentEvalRunner", "CommandExecutor", "TaskExecution", "TaskExecutor"]
