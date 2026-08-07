"""Joint replay of a parent run and the child sessions it delegated to.

A subagent runs in its own session so the parent's log keeps a single writer
(ADR-0023). The cost is that neither session alone shows the whole story. A
`TraceGraph` composes the per-session bundles instead of relaxing them: every
`TraceBundle` keeps its single-session hash and sequence guarantees, and the
graph adds the links between them.

Linkage is verified, not assumed. A child that names a parent this graph does
not contain, a delegation without an outcome, or a duplicated task is rejected
rather than replayed as if it were complete.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from aiharness.core.events import Event
from aiharness.evals.errors import EvalValidationError
from aiharness.evals.replay import ReplayEngine, ReplayResult, TraceBundle
from aiharness.observability import Redactor


@dataclass(frozen=True, slots=True)
class Delegation:
    """One parent run handing a task to one child session."""

    task_id: str
    parent_session_id: str
    parent_run_id: str
    child_session_id: str
    child_run_id: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "parent_session_id": self.parent_session_id,
            "parent_run_id": self.parent_run_id,
            "child_session_id": self.child_session_id,
            "child_run_id": self.child_run_id,
            "state": self.state,
        }


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalValidationError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class TraceGraph:
    """A root session bundle plus the child sessions it spawned."""

    root: TraceBundle
    children: tuple[TraceBundle, ...] = ()

    def __post_init__(self) -> None:
        seen = {self.root.session_id}
        for child in self.children:
            if child.session_id in seen:
                raise EvalValidationError(
                    f"Duplicate session in trace graph: {child.session_id}"
                )
            seen.add(child.session_id)
        # Building the delegations validates the links.
        object.__setattr__(self, "children", tuple(self.children))
        self.delegations()

    @classmethod
    def from_sessions(
        cls,
        root: Iterable[Event],
        children: Iterable[Iterable[Event]] = (),
        *,
        redactor: Redactor | None = None,
    ) -> TraceGraph:
        return cls(
            root=TraceBundle.from_events(root, redactor=redactor),
            children=tuple(
                TraceBundle.from_events(events, redactor=redactor) for events in children
            ),
        )

    def delegations(self) -> tuple[Delegation, ...]:
        """Every verified parent → child link, in child session order."""

        parent_runs = self._root_run_ids()
        found: list[Delegation] = []
        tasks: set[str] = set()
        for child in self.children:
            started = self._single(child, "subagent.started")
            completed = self._single(child, "subagent.completed")
            task_id = _text(started.get("task_id"), "subagent.started task_id")
            if _text(completed.get("task_id"), "subagent.completed task_id") != task_id:
                raise EvalValidationError(
                    f"Child session {child.session_id} starts and completes different tasks"
                )
            if task_id in tasks:
                raise EvalValidationError(f"Duplicate delegated task: {task_id}")
            tasks.add(task_id)
            parent_session = _text(started.get("parent_session_id"), "parent_session_id")
            if parent_session != self.root.session_id:
                raise EvalValidationError(
                    f"Child session {child.session_id} names a parent outside this graph"
                )
            parent_run = _text(started.get("parent_run_id"), "parent_run_id")
            if parent_run not in parent_runs:
                raise EvalValidationError(
                    f"Child session {child.session_id} names an unknown parent run"
                )
            result = completed.get("result")
            if not isinstance(result, Mapping):
                raise EvalValidationError(
                    f"Child session {child.session_id} completed without a result"
                )
            found.append(
                Delegation(
                    task_id=task_id,
                    parent_session_id=parent_session,
                    parent_run_id=parent_run,
                    child_session_id=child.session_id,
                    child_run_id=_text(started.get("child_run_id"), "child_run_id"),
                    state=_text(result.get("state"), "subagent result state"),
                )
            )
        return tuple(found)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "root": self.root.to_dict(),
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TraceGraph:
        if not isinstance(value, Mapping):
            raise EvalValidationError("TraceGraph must be a JSON object")
        if value.get("schema_version", 1) != 1:
            raise EvalValidationError("Unsupported TraceGraph schema version")
        raw_root = value.get("root")
        raw_children = value.get("children", [])
        if not isinstance(raw_root, Mapping) or not isinstance(raw_children, list):
            raise EvalValidationError("TraceGraph needs a root bundle and a children list")
        return cls(
            root=TraceBundle.from_dict(raw_root),
            children=tuple(TraceBundle.from_dict(child) for child in raw_children),
        )

    def _root_run_ids(self) -> frozenset[str]:
        return frozenset(
            str(event["run_id"])
            for event in self.root.events
            if isinstance(event, Mapping) and event.get("run_id")
        )

    @staticmethod
    def _single(child: TraceBundle, event_type: str) -> Mapping[str, object]:
        matches = [
            event
            for event in child.events
            if isinstance(event, Mapping) and event.get("type") == event_type
        ]
        if len(matches) != 1:
            raise EvalValidationError(
                f"Child session {child.session_id} must hold exactly one {event_type}"
            )
        data = matches[0].get("data")
        if not isinstance(data, Mapping):
            raise EvalValidationError(f"{event_type} must carry an object payload")
        return data


@dataclass(frozen=True, slots=True)
class GraphReplayResult:
    """Per-session replay plus the delegation structure that ties them."""

    root: ReplayResult
    children: tuple[ReplayResult, ...]
    delegations: tuple[Delegation, ...]
    state_sha256: str = ""
    event_count: int = 0
    pending_tool_call_ids: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root.to_dict(),
            "children": [child.to_dict() for child in self.children],
            "delegations": [item.to_dict() for item in self.delegations],
            "event_count": self.event_count,
            "pending_tool_call_ids": list(self.pending_tool_call_ids),
            "state_sha256": self.state_sha256,
        }


def replay_graph(graph: TraceGraph, *, engine: ReplayEngine | None = None) -> GraphReplayResult:
    """Replay every session in the graph and report them as one outcome."""

    replay = engine or ReplayEngine()
    root = replay.replay(graph.root)
    children = tuple(replay.replay(child) for child in graph.children)
    delegations = graph.delegations()
    pending = list(root.pending_tool_call_ids)
    for child in children:
        pending.extend(child.pending_tool_call_ids)
    payload = {
        "root": root.to_dict(),
        "children": [child.to_dict() for child in children],
        "delegations": [item.to_dict() for item in delegations],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return GraphReplayResult(
        root=root,
        children=children,
        delegations=delegations,
        state_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        event_count=root.event_count + sum(child.event_count for child in children),
        pending_tool_call_ids=tuple(pending),
    )


__all__ = ["Delegation", "GraphReplayResult", "TraceGraph", "replay_graph"]
