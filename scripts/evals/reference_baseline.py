"""Deterministic reference executor for the PR smoke gate.

This executor is intentionally a tooling fixture. It validates the task,
workspace, oracle and report chain; it is not a model baseline.
"""

from __future__ import annotations

from pathlib import Path

from aihi.agent import Event, EventStore, RunResult, RunState
from aihi.code_agent import create_coding_session
from aihi.code_agent.evals import CodeTask, TaskExecution


async def reference_executor(
    task: CodeTask, workspace: Path, store: EventStore
) -> TaskExecution:
    patches = {
        "bug-fix-bool": (
            "def parse_bool(value: str) -> bool:\n"
            "    normalized = value.strip().lower()\n"
            "    if normalized in {'', 'false', '0', 'no', 'off'}:\n"
            "        return False\n"
            "    if normalized in {'true', '1', 'yes', 'on'}:\n"
            "        return True\n"
            "    raise ValueError('not a boolean')\n"
        ),
        "feature-slug": (
            "import re\n"
            "import unicodedata\n\n"
            "def slugify(value: str) -> str:\n"
            "    ascii_value = unicodedata.normalize(\n"
            "        'NFKD', value\n"
            "    ).encode('ascii', 'ignore').decode()\n"
            "    return re.sub(r'[^a-z0-9]+', '-', ascii_value.lower()).strip('-')\n"
        ),
        "test-repair-stats": (
            "def median(values: list[float]) -> float:\n"
            "    ordered = sorted(values)\n"
            "    middle = len(ordered) // 2\n"
            "    if len(ordered) % 2:\n"
            "        return ordered[middle]\n"
            "    return (ordered[middle - 1] + ordered[middle]) / 2\n"
        ),
        "security-safe-path": (
            "from pathlib import Path\n\n"
            "def resolve_inside(root: str | Path, candidate: str) -> Path:\n"
            "    root_path = Path(root).resolve()\n"
            "    candidate_path = (root_path / candidate).resolve()\n"
            "    try:\n"
            "        candidate_path.relative_to(root_path)\n"
            "    except ValueError as exc:\n"
            "        raise ValueError('path escapes root') from exc\n"
            "    return candidate_path\n"
        ),
        "refactor-name": (
            "def _join_parts(parts: list[str]) -> str:\n"
            "    return \" \".join(parts)\n\n"
            "\n"
            "def format_label(first: str, last: str) -> str:\n"
            "    \"\"\"Format a two-part label without changing caller-visible spacing.\"\"\"\n\n"
            "    return _join_parts([first, last])\n"
        ),
        "repository-understanding-settings": (
            "DEFAULTS = {\"timeout\": 30, \"retries\": 2}\n\n\n"
            "def get_setting(config: dict[str, int], name: str) -> int | None:\n"
            "    \"\"\"Read a setting and fall back to the known defaults.\"\"\"\n\n"
            "    return config.get(name, DEFAULTS.get(name))\n\n\n"
            "def effective_timeout(config: dict[str, object]) -> int:\n"
            "    candidate = config.get(\"timeout\")\n"
            "    if (\n"
            "        isinstance(candidate, int)\n"
            "        and not isinstance(candidate, bool)\n"
            "        and candidate > 0\n"
            "    ):\n"
            "        return candidate\n"
            "    return DEFAULTS[\"timeout\"]\n"
        ),
        "instruction-following-report": (
            "# Changelog\n\n- Added the requested report.\n"
        ),
        "interrupt-resume-checkpoint": (
            "def resume_offset(state: dict[str, int]) -> int:\n"
            "    \"\"\"Return the saved offset for a resumable run.\"\"\"\n\n"
            "    value = state.get(\"offset\", 0)\n"
            "    return value if value > 0 else 0\n"
        ),
        "subagent-plan": (
            "def plan_children(items: list[str]) -> list[str]:\n"
            "    \"\"\"Build the child-task list for a delegated run.\"\"\"\n\n"
            "    return [item.strip() for item in items if item.strip()]\n"
        ),
    }
    targets = {
        "bug-fix-bool": "target.py",
        "feature-slug": "slug.py",
        "test-repair-stats": "stats.py",
        "security-safe-path": "safe_path.py",
        "refactor-name": "formatter.py",
        "repository-understanding-settings": "settings.py",
        "instruction-following-report": "CHANGELOG.md",
        "interrupt-resume-checkpoint": "checkpoint.py",
        "subagent-plan": "delegation_plan.py",
    }
    try:
        target = targets[task.case_id]
        patch = patches[task.case_id]
    except KeyError as exc:
        raise ValueError(f"No reference patch for task {task.case_id}") from exc
    (workspace / target).write_text(patch, encoding="utf-8")
    if task.case_id == "test-repair-stats":
        (workspace / "test_stats.py").write_text(
            "from stats import median\n\nassert median([1, 2, 3, 4]) == 2.5\n",
            encoding="utf-8",
        )

    session = create_coding_session(
        store, cwd=workspace, provider="reference", model="reference"
    )
    session.append_many(
        [
            Event(type="run.started", session_id=session.id, run_id="reference-run"),
            Event(
                type="run.state_changed",
                session_id=session.id,
                run_id="reference-run",
                data={"state": "running"},
            ),
            Event(
                type="run.state_changed",
                session_id=session.id,
                run_id="reference-run",
                data={"state": "completed"},
            ),
            Event(
                type="run.completed",
                session_id=session.id,
                run_id="reference-run",
                data={"state": "completed"},
            ),
        ]
    )
    return TaskExecution(
        session=session,
        run_result=RunResult(run_id="reference-run", state=RunState.COMPLETED),
    )


__all__ = ["reference_executor"]
