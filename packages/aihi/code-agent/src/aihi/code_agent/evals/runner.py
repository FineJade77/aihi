"""Isolated Coding Agent task execution and deterministic post-run grading."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from aihi.agent import (
    AgentRuntimeError,
    DockerBackend,
    EventStore,
    InMemoryEventStore,
    Session,
)
from aihi.agent.evals import TraceBundle
from aihi.agent.observability import Redactor
from aihi.agent.runtime import RunResult
from aihi.agent.sandbox.docker import DockerRunner
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
from aihi.code_agent.prompts import build_system_prompt, load_builtin_prompt
from aihi.code_agent.runtime import CodeAgentRuntime
from aihi.models import Message

_MAX_COMMAND_OUTPUT = 4_096


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """The durable Session produced by an injected or configured executor."""

    session: Session
    run_result: RunResult | None = None
    metrics: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        json.dumps(self.metrics, ensure_ascii=False, allow_nan=False)


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
        self.command_executor = self._resolve_command_executor(command_executor)
        self._tools_sha256: str | None = None

    def _resolve_command_executor(
        self, command_executor: CommandExecutor | None
    ) -> CommandExecutor:
        """Never grade Provider-written code in the evaluation process.

        The Agent already writes its patch inside the Docker sandbox.  Running
        the oracle on the host would import those files back into the host
        interpreter, which is a sandbox bypass, so a configured (live) run
        grades inside its own disposable container.  The scripted PR executor
        only replays patches committed to this repository and keeps the host
        executor so the deterministic gate does not require a daemon.
        """

        if self.config is None:
            return command_executor or run_command_on_host
        image = self.config.sandbox.image
        if not isinstance(image, str) or not image.strip():
            raise CodeAgentConfigError(
                "benchmark oracle execution requires sandbox.image so hidden tests "
                "run in a container instead of on the host"
            )
        if command_executor is None:
            return DockerCommandExecutor(image=image.strip())
        if command_executor is run_command_on_host:
            raise CodeAgentConfigError(
                "the host oracle executor cannot grade Provider-written code; "
                "hidden tests must run in the Docker sandbox"
            )
        return command_executor

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
                if execution.run_result is not None:
                    run_state = execution.run_result.state.value
                    elapsed = time.perf_counter() - started
                    if run_state == "interrupted" and elapsed >= task.timeout_seconds:
                        # Runtime repairs cancellation into a durable INTERRUPTED result.
                        # asyncio.wait_for therefore returns normally after its deadline.
                        execution_error = "execution_timeout"
                    elif run_state != "completed":
                        execution_error = f"execution_{run_state}"
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
            if execution is not None:
                metrics.update(execution.metrics)
                metrics.update(_session_metrics(execution))
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
        tool_payload = [
            spec.model_definition.to_dict() for spec in runtime.runtime.registry.specs
        ]
        encoded_tools = json.dumps(
            tool_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tools_sha256 = hashlib.sha256(encoded_tools).hexdigest()
        if self._tools_sha256 is not None and self._tools_sha256 != tools_sha256:
            await runtime.close()
            raise CodeAgentConfigError("benchmark tool definitions changed between attempts")
        self._tools_sha256 = tools_sha256
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
        result: dict[str, object] = {
            "provider": self.config.provider.name,
            "model": self.config.provider.model,
            "oracle_execution": self._oracle_execution(),
            "prompt_sha256": hashlib.sha256(
                load_builtin_prompt().encode("utf-8")
            ).hexdigest(),
        }
        if self._tools_sha256 is not None:
            result["tools_sha256"] = self._tools_sha256
        return result

    def _oracle_execution(self) -> str:
        """Describe where hidden tests ran so a report cannot hide host grading."""

        executor = self.command_executor
        if isinstance(executor, DockerCommandExecutor):
            return f"docker:{executor.image}"
        if executor is run_command_on_host:
            return "host"
        return "injected"


def _session_metrics(execution: TaskExecution) -> dict[str, object]:
    """Aggregate durable usage/tool events for one benchmark attempt."""

    run_id = execution.run_result.run_id if execution.run_result is not None else None
    events = tuple(
        event
        for event in execution.session.events
        if run_id is None or event.run_id == run_id
    )
    usage_events = tuple(event for event in events if event.type == "model.usage")
    compaction_events = tuple(
        event for event in events if event.type == "compaction.created"
    )

    def token_total(name: str) -> int:
        total = 0
        for event in usage_events:
            raw = event.data.get(name)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                total += raw
        return total

    input_tokens = token_total("input_tokens")
    output_tokens = token_total("output_tokens")
    cached_input_tokens = token_total("cached_input_tokens")
    cache_key_hashes = tuple(
        value
        for event in usage_events
        if isinstance((value := event.data.get("cache_key_hash")), str) and value
    )
    cache_key_change_count = sum(
        current != previous
        for previous, current in zip(cache_key_hashes, cache_key_hashes[1:], strict=False)
    )
    metrics: dict[str, object] = {
        "model_calls": len(usage_events),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": token_total("cache_write_input_tokens"),
        "cache_hit_ratio": (
            cached_input_tokens / input_tokens if input_tokens else 0.0
        ),
        "cache_key_change_count": cache_key_change_count,
        "compaction_count": len(compaction_events),
        "hard_compaction_count": sum(
            event.data.get("version") == 2 for event in compaction_events
        ),
        "soft_compaction_count": sum(
            isinstance(event.data.get("context_pruned_tool_results"), int)
            and event.data.get("context_pruned_tool_results", 0) > 0
            for event in usage_events
        ),
        "tokens": input_tokens + output_tokens,
        "tool_calls": sum(event.type == "tool.started" for event in events),
    }
    if cache_key_hashes:
        metrics["cache_key_hash"] = cache_key_hashes[-1]
    costs = [
        float(raw)
        for event in usage_events
        if isinstance((raw := event.data.get("cost_usd")), (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(raw)
        and raw >= 0
    ]
    if costs:
        metrics["cost_usd"] = sum(costs)
    return metrics


@dataclass(frozen=True, slots=True)
class DockerCommandExecutor:
    """Run oracle commands in a disposable, network-isolated container.

    One container per command.  The workspace is the only host path mounted,
    the root filesystem is read-only, all capabilities are dropped and no
    network is available, so a hidden test never executes model-written code in
    the evaluation process.
    """

    image: str
    runner: DockerRunner | None = None
    workspace_read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise CodeAgentConfigError("oracle container requires a non-empty image")

    async def __call__(
        self, argv: tuple[str, ...], cwd: Path, timeout: float
    ) -> CommandOutcome:
        started = time.perf_counter()
        try:
            backend = DockerBackend(
                cwd,
                image=self.image,
                runner=self.runner,
                network="none",
                allow_network=False,
                workspace_read_only=self.workspace_read_only,
            )
            result = await backend.run_command(
                argv, timeout_seconds=timeout, max_output_chars=_MAX_COMMAND_OUTPUT
            )
        except (AgentRuntimeError, OSError, ValueError) as exc:
            # A container that cannot start is a failed grade, not a crashed run.
            return CommandOutcome(
                argv=argv,
                exit_code=None,
                stderr=f"{type(exc).__name__}: oracle container could not run the command",
                duration_seconds=time.perf_counter() - started,
            )
        redactor = Redactor(max_string=_MAX_COMMAND_OUTPUT)
        return CommandOutcome(
            argv=argv,
            exit_code=None if result.timed_out else result.exit_code,
            stdout=str(redactor.redact(result.stdout)),
            stderr=str(redactor.redact(result.stderr)),
            timed_out=result.timed_out,
            duration_seconds=time.perf_counter() - started,
        )


async def run_command_on_host(
    argv: tuple[str, ...], cwd: Path, timeout: float
) -> CommandOutcome:
    """Run an oracle command directly on the host.

    Only valid for the scripted reference executor, whose patches are committed
    to this repository.  A live run must use :class:`DockerCommandExecutor`.
    """

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


__all__ = [
    "CodeAgentEvalRunner",
    "CommandExecutor",
    "DockerCommandExecutor",
    "TaskExecution",
    "TaskExecutor",
    "run_command_on_host",
]
