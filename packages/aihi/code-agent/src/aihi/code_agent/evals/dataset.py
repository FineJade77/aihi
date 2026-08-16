"""Versioned task definitions for offline and isolated Coding Agent evals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = frozenset(
    {
        "bug_fix",
        "feature",
        "test_repair",
        "refactor",
        "repository_understanding",
        "instruction_following",
        "security_boundary",
        "interrupt_resume",
        "subagent",
    }
)


class CodeEvalValidationError(ValueError):
    """A task or dataset violates the versioned evaluation contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeEvalValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CodeEvalValidationError(f"{name} must be a positive integer")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise CodeEvalValidationError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _strict_json(value: object, name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CodeEvalValidationError(f"{name} must be strict JSON") from exc


@dataclass(frozen=True, slots=True)
class CodeTask:
    """One deterministic Coding Agent task and its post-run oracle."""

    case_id: str
    category: str
    prompt: str
    fixture_path: Path
    fixture_sha256: str
    timeout_seconds: int
    max_turns: int
    max_tokens: int
    test_commands: tuple[tuple[str, ...], ...]
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    require_clean_regression: bool
    expected_files: tuple[str, ...] = ()
    sandbox_backend: str = "docker"
    network: bool = False
    repeat: int = 1
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        case_id = _text(self.case_id, "case_id")
        object.__setattr__(self, "case_id", case_id)
        category = _text(self.category, "category")
        if category not in _CATEGORIES:
            raise CodeEvalValidationError(f"unsupported task category: {category}")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "prompt", _text(self.prompt, "prompt"))
        path = self.fixture_path.expanduser().resolve()
        if not path.is_dir():
            raise CodeEvalValidationError(f"fixture path is not a directory: {path}")
        object.__setattr__(self, "fixture_path", path)
        if not _SHA256.fullmatch(self.fixture_sha256):
            raise CodeEvalValidationError("fixture_sha256 must be lowercase SHA-256")
        for name in ("timeout_seconds", "max_turns", "max_tokens", "repeat"):
            _positive_int(getattr(self, name), name)
        if not self.test_commands or any(
            not command or any(not isinstance(part, str) or not part for part in command)
            for command in self.test_commands
        ):
            raise CodeEvalValidationError("test_commands must contain non-empty argv arrays")
        for name, values in (
            ("allowed_paths", self.allowed_paths),
            ("forbidden_paths", self.forbidden_paths),
            ("expected_files", self.expected_files),
        ):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise CodeEvalValidationError(f"{name} must contain non-empty strings")
        if self.sandbox_backend != "docker":
            raise CodeEvalValidationError("sandbox_backend must be docker for benchmark tasks")
        if self.network is not False:
            raise CodeEvalValidationError("benchmark tasks must disable network")
        if not isinstance(self.require_clean_regression, bool):
            raise CodeEvalValidationError("require_clean_regression must be boolean")
        _strict_json(self.metadata, "metadata")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, base_dir: str | Path) -> CodeTask:
        if not isinstance(value, Mapping):
            raise CodeEvalValidationError("CodeTask must be an object")
        case_id = _text(value.get("case_id"), "case_id")
        fixture = value.get("fixture")
        limits = value.get("limits")
        oracle = value.get("oracle")
        if not isinstance(fixture, Mapping):
            raise CodeEvalValidationError("fixture must be an object")
        if not isinstance(limits, Mapping):
            raise CodeEvalValidationError("limits must be an object")
        if not isinstance(oracle, Mapping):
            raise CodeEvalValidationError("oracle must be an object")
        raw_path = _text(fixture.get("path"), "fixture.path")
        base = Path(base_dir).expanduser().resolve(strict=True)
        fixture_path = (
            (base / raw_path).resolve()
            if not Path(raw_path).is_absolute()
            else Path(raw_path).resolve()
        )
        try:
            fixture_path.relative_to(base)
        except ValueError as exc:
            raise CodeEvalValidationError("fixture.path must stay inside the dataset root") from exc
        commands = oracle.get("test_commands")
        if not isinstance(commands, list):
            raise CodeEvalValidationError("oracle.test_commands must be a list")
        test_commands = tuple(
            _string_list(command, "oracle.test_commands item") for command in commands
        )
        execution = value.get("execution", {})
        if not isinstance(execution, Mapping):
            raise CodeEvalValidationError("execution must be an object")
        return cls(
            case_id=case_id,
            category=_text(value.get("category"), "category"),
            prompt=_text(value.get("prompt"), "prompt"),
            fixture_path=fixture_path,
            fixture_sha256=_text(fixture.get("sha256"), "fixture.sha256"),
            timeout_seconds=_positive_int(limits.get("timeout_seconds"), "limits.timeout_seconds"),
            max_turns=_positive_int(limits.get("max_turns"), "limits.max_turns"),
            max_tokens=_positive_int(limits.get("max_tokens"), "limits.max_tokens"),
            test_commands=test_commands,
            allowed_paths=_string_list(oracle.get("allowed_paths", []), "oracle.allowed_paths"),
            forbidden_paths=_string_list(
                oracle.get("forbidden_paths", []), "oracle.forbidden_paths"
            ),
            require_clean_regression=oracle.get("require_clean_regression") is True,
            expected_files=_string_list(
                oracle.get("expected_files", []), "oracle.expected_files"
            ),
            sandbox_backend=str(execution.get("sandbox_backend", "docker")),
            network=execution.get("network", False) is True,
            repeat=_positive_int(execution.get("repeat", 1), "execution.repeat"),
            metadata=dict(value.get("metadata", {}))
            if isinstance(value.get("metadata", {}), Mapping)
            else {},
        )

    def to_dict(self, *, base_dir: str | Path | None = None) -> dict[str, object]:
        fixture_path = self.fixture_path
        if base_dir is not None:
            try:
                fixture_path = fixture_path.relative_to(Path(base_dir).expanduser().resolve())
            except ValueError:
                pass
        return {
            "case_id": self.case_id,
            "category": self.category,
            "prompt": self.prompt,
            "fixture": {"path": fixture_path.as_posix(), "sha256": self.fixture_sha256},
            "limits": {
                "timeout_seconds": self.timeout_seconds,
                "max_turns": self.max_turns,
                "max_tokens": self.max_tokens,
            },
            "oracle": {
                "test_commands": [list(command) for command in self.test_commands],
                "allowed_paths": list(self.allowed_paths),
                "forbidden_paths": list(self.forbidden_paths),
                "require_clean_regression": self.require_clean_regression,
                "expected_files": list(self.expected_files),
            },
            "execution": {
                "sandbox_backend": self.sandbox_backend,
                "network": self.network,
                "repeat": self.repeat,
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeTaskDataset:
    dataset_id: str
    tasks: tuple[CodeTask, ...]

    def __post_init__(self) -> None:
        dataset_id = _text(self.dataset_id, "dataset_id")
        object.__setattr__(self, "dataset_id", dataset_id)
        ids = [task.case_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise CodeEvalValidationError("task case ids must be unique")

    @classmethod
    def from_jsonl(
        cls, dataset_id: str, text: str, *, base_dir: str | Path
    ) -> CodeTaskDataset:
        tasks: list[CodeTask] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodeEvalValidationError(f"invalid task JSON at line {line_number}") from exc
            if not isinstance(value, dict):
                raise CodeEvalValidationError(f"task line {line_number} must be an object")
            tasks.append(cls._from_dict(value, base_dir=base_dir))
        return cls(dataset_id, tuple(tasks))

    @staticmethod
    def _from_dict(value: dict[str, Any], *, base_dir: str | Path) -> CodeTask:
        return CodeTask.from_dict(value, base_dir=base_dir)

    def to_jsonl(self, *, base_dir: str | Path | None = None) -> str:
        return "\n".join(
            json.dumps(task.to_dict(base_dir=base_dir), ensure_ascii=False, sort_keys=True)
            for task in self.tasks
        )


def directory_sha256(path: str | Path) -> str:
    """Hash fixture file names and bytes deterministically, rejecting symlinks."""

    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise CodeEvalValidationError(f"fixture path is not a directory: {root}")
    digest = hashlib.sha256()
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise CodeEvalValidationError(f"fixture contains a symlink: {candidate}")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "CodeEvalValidationError",
    "CodeTask",
    "CodeTaskDataset",
    "directory_sha256",
]
