"""Canonical, JSON-serializable subagent task types.

The types in this module intentionally describe *authority* and durable state,
not a worker implementation.  A future worker backend must consume these
types and may never enlarge a child task's authority.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from aihi.agent._core.events import utc_now
from aihi.agent._core.ids import new_id

from .errors import AgentValidationError


def _text(value: object, name: str, *, max_length: int = 4_096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentValidationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise AgentValidationError(f"{name} exceeds {max_length} characters")
    return result


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentValidationError(f"{name} must be an integer")
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    """Narrow one snapshot field to a JSON object without copying it."""

    if not isinstance(value, dict):
        raise AgentValidationError(f"{name} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _capabilities(value: object) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, (set, frozenset, tuple, list)):
        raise AgentValidationError("capabilities must be a collection of strings")
    result = frozenset(_text(item, "capability", max_length=256) for item in value)
    return result


def _strings(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (set, frozenset, tuple, list)):
        raise AgentValidationError(f"{name} must be a collection of strings")
    return tuple(_text(item, name) for item in value)


def _json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentValidationError(f"{name} must be a JSON object")
    # json.dumps is deliberately used as a validation pass without importing
    # provider-specific types into core.  NaN/Infinity are not JSON values.
    import json

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AgentValidationError(f"{name} must be JSON serializable") from exc
    return deepcopy(value)


class AgentState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """Upper bounds inherited by a child task."""

    max_tokens: int = 8_192
    max_cost_usd: float | None = None
    timeout_seconds: float = 600.0
    max_tool_calls: int = 100

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise AgentValidationError("max_tokens must be a positive integer")
        if self.max_cost_usd is not None and (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(self.max_cost_usd)
            or self.max_cost_usd < 0
        ):
            raise AgentValidationError("max_cost_usd must be a finite, non-negative number")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise AgentValidationError("timeout_seconds must be finite and positive")
        if (
            isinstance(self.max_tool_calls, bool)
            or not isinstance(self.max_tool_calls, int)
            or self.max_tool_calls <= 0
        ):
            raise AgentValidationError("max_tool_calls must be a positive integer")

    def is_subset_of(self, parent: AgentBudget) -> bool:
        cost_ok = parent.max_cost_usd is None or (
            self.max_cost_usd is not None and self.max_cost_usd <= parent.max_cost_usd
        )
        return (
            self.max_tokens <= parent.max_tokens
            and cost_ok
            and self.timeout_seconds <= parent.timeout_seconds
            and self.max_tool_calls <= parent.max_tool_calls
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> AgentBudget:
        if not isinstance(value, dict):
            raise AgentValidationError("budget must be a JSON object")
        required = {"max_tokens", "max_cost_usd", "timeout_seconds", "max_tool_calls"}
        if not required.issubset(value):
            raise AgentValidationError("budget is missing required fields")
        max_tokens = value["max_tokens"]
        max_cost = value["max_cost_usd"]
        timeout = value["timeout_seconds"]
        max_tool_calls = value["max_tool_calls"]
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or (
                max_cost is not None
                and (isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)))
            )
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or isinstance(max_tool_calls, bool)
            or not isinstance(max_tool_calls, int)
        ):
            raise AgentValidationError("budget fields have invalid types")
        return cls(
            max_tokens=max_tokens,
            max_cost_usd=max_cost,
            timeout_seconds=timeout,
            max_tool_calls=max_tool_calls,
        )


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A durable subagent request and its inherited authority."""

    parent_run_id: str
    objective: str
    budget: AgentBudget
    task_id: str = field(default_factory=lambda: new_id("task"))
    child_run_id: str = field(default_factory=lambda: new_id("run"))
    parent_task_id: str | None = None
    constraints: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    depth: int = 0
    max_depth: int = 4
    max_children: int = 8
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "parent_run_id", _text(self.parent_run_id, "parent_run_id", max_length=256)
        )
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id", max_length=256))
        object.__setattr__(
            self, "child_run_id", _text(self.child_run_id, "child_run_id", max_length=256)
        )
        if not isinstance(self.created_at, str) or not self.created_at:
            raise AgentValidationError("created_at must be a non-empty string")
        if self.parent_task_id is not None:
            object.__setattr__(
                self, "parent_task_id", _text(self.parent_task_id, "parent_task_id", max_length=256)
            )
        object.__setattr__(self, "constraints", _strings(self.constraints, "constraint"))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        if isinstance(self.depth, bool) or not isinstance(self.depth, int) or self.depth < 0:
            raise AgentValidationError("depth must be a non-negative integer")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or self.max_depth < self.depth
        ):
            raise AgentValidationError("max_depth must be at least depth")
        if (
            isinstance(self.max_children, bool)
            or not isinstance(self.max_children, int)
            or self.max_children < 0
        ):
            raise AgentValidationError("max_children must be non-negative")
        object.__setattr__(self, "metadata", _json_object(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_run_id": self.parent_run_id,
            "objective": self.objective,
            "budget": self.budget.to_dict(),
            "task_id": self.task_id,
            "child_run_id": self.child_run_id,
            "parent_task_id": self.parent_task_id,
            "constraints": list(self.constraints),
            "capabilities": sorted(self.capabilities),
            "depth": self.depth,
            "max_depth": self.max_depth,
            "max_children": self.max_children,
            "metadata": deepcopy(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TaskSpec:
        if not isinstance(value, dict):
            raise AgentValidationError("task spec must be a JSON object")
        required = {
            "parent_run_id",
            "objective",
            "budget",
            "task_id",
            "child_run_id",
        }
        if not required.issubset(value):
            raise AgentValidationError("task spec is missing required fields")
        identity_fields = {"parent_run_id", "objective", "task_id", "child_run_id"}
        if any(not isinstance(value.get(key), str) for key in identity_fields):
            raise AgentValidationError("task spec identity fields must be strings")
        constraints = value.get("constraints", [])
        capabilities = value.get("capabilities", [])
        if not isinstance(constraints, (list, tuple)) or not isinstance(
            capabilities, (list, tuple, set, frozenset)
        ):
            raise AgentValidationError("task constraints and capabilities must be collections")
        if any(not isinstance(item, str) for item in constraints):
            raise AgentValidationError("task constraints must contain strings")
        if any(not isinstance(item, str) for item in capabilities):
            raise AgentValidationError("task capabilities must contain strings")
        parent_task_id = value.get("parent_task_id")
        if parent_task_id is not None and not isinstance(parent_task_id, str):
            raise AgentValidationError("parent_task_id must be a string or null")
        metadata = _mapping(value.get("metadata", {}), "task metadata")
        created_at = value.get("created_at", utc_now())
        if not isinstance(created_at, str):
            raise AgentValidationError("task created_at must be a string")
        return cls(
            parent_run_id=_text(value["parent_run_id"], "parent_run_id"),
            objective=_text(value["objective"], "objective"),
            budget=AgentBudget.from_dict(_mapping(value["budget"], "task budget")),
            task_id=_text(value["task_id"], "task_id"),
            child_run_id=_text(value["child_run_id"], "child_run_id"),
            parent_task_id=parent_task_id,
            constraints=tuple(str(item) for item in constraints),
            capabilities=frozenset(str(item) for item in capabilities),
            depth=_int(value.get("depth", 0), "task depth"),
            max_depth=_int(value.get("max_depth", 4), "task max_depth"),
            max_children=_int(value.get("max_children", 8), "task max_children"),
            metadata=metadata,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    state: AgentState
    summary: str = ""
    output_artifact_ids: tuple[str, ...] = ()
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id", max_length=256))
        if not isinstance(self.state, AgentState):
            object.__setattr__(self, "state", AgentState(str(self.state)))
        if not isinstance(self.summary, str):
            raise AgentValidationError("summary must be a string")
        if len(self.summary) > 4_096:
            raise AgentValidationError("summary exceeds 4096 characters")
        if isinstance(self.output_artifact_ids, str) or not isinstance(
            self.output_artifact_ids, (set, frozenset, tuple, list)
        ):
            raise AgentValidationError("output_artifact_ids must be a collection")
        object.__setattr__(
            self,
            "output_artifact_ids",
            tuple(
                _text(item, "output_artifact_id", max_length=256)
                for item in self.output_artifact_ids
            ),
        )
        if self.error is not None and not isinstance(self.error, str):
            raise AgentValidationError("error must be a string")
        if self.error is not None and len(self.error) > 4_096:
            raise AgentValidationError("error exceeds 4096 characters")
        object.__setattr__(self, "metrics", _json_object(self.metrics, "metrics"))

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "summary": self.summary,
            "output_artifact_ids": list(self.output_artifact_ids),
            "error": self.error,
            "metrics": deepcopy(self.metrics),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TaskResult:
        if not isinstance(value, dict):
            raise AgentValidationError("task result must be an object")
        artifacts = value.get("output_artifact_ids", [])
        if not isinstance(artifacts, (list, tuple)) or any(
            not isinstance(item, str) for item in artifacts
        ):
            raise AgentValidationError("output_artifact_ids must be a collection")
        task_id = value.get("task_id")
        state = value.get("state")
        summary = value.get("summary", "")
        error = value.get("error")
        metrics = value.get("metrics", {})
        if not isinstance(task_id, str) or not isinstance(state, str):
            raise AgentValidationError("task result identity fields must be strings")
        if not isinstance(summary, str) or (error is not None and not isinstance(error, str)):
            raise AgentValidationError("task result text fields have invalid types")
        if not isinstance(metrics, dict):
            raise AgentValidationError("task result metrics must be an object")
        return cls(
            task_id=task_id,
            state=AgentState(state),
            summary=summary,
            output_artifact_ids=tuple(artifacts),
            error=error,
            metrics=metrics,
        )


@dataclass(frozen=True, slots=True)
class TaskNode:
    spec: TaskSpec
    state: AgentState = AgentState.PENDING
    child_task_ids: tuple[str, ...] = ()
    result: TaskResult | None = None
    reason: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec.to_dict(),
            "state": self.state.value,
            "child_task_ids": list(self.child_task_ids),
            "result": self.result.to_dict() if self.result else None,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TaskNode:
        if not isinstance(value, dict) or not isinstance(value.get("spec"), dict):
            raise AgentValidationError("Task node spec must be an object")
        children = value.get("child_task_ids", [])
        if not isinstance(children, (list, tuple)) or any(
            not isinstance(item, str) for item in children
        ):
            raise AgentValidationError("child_task_ids must be a collection")
        raw_result = value.get("result")
        if raw_result is not None and not isinstance(raw_result, dict):
            raise AgentValidationError("Task node result must be an object or null")
        reason = value.get("reason")
        updated_at = value.get("updated_at", utc_now())
        if reason is not None and not isinstance(reason, str):
            raise AgentValidationError("task node reason must be a string or null")
        if not isinstance(updated_at, str):
            raise AgentValidationError("task node updated_at must be a string")
        return cls(
            spec=TaskSpec.from_dict(_mapping(value["spec"], "task node spec")),
            state=AgentState(_text(value.get("state", AgentState.PENDING.value), "task state")),
            child_task_ids=tuple(children),
            result=TaskResult.from_dict(raw_result) if isinstance(raw_result, dict) else None,
            reason=reason,
            updated_at=updated_at,
        )
