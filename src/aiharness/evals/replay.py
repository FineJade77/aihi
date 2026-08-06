"""Redacted trace bundles and deterministic, side-effect-free event replay."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from aiharness.core.events import Event
from aiharness.core.types import Message
from aiharness.evals.errors import EvalValidationError, ReplayInvariantViolation
from aiharness.observability import Redactor
from aiharness.runtime.state import InvalidRunTransition, RunState, RunStateMachine


def _text(value: object, name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalValidationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise EvalValidationError(f"{name} exceeds {max_length} characters")
    return result


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _exact_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            _thaw(value), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise EvalValidationError("TraceBundle events must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TraceBundle:
    """Portable redacted event input for replay and offline evaluation."""

    session_id: str
    events: tuple[dict[str, object], ...]
    source_sha256: str
    schema_version: int = 1
    redacted: bool = True

    def __post_init__(self) -> None:
        session_id = _text(self.session_id, "session_id")
        object.__setattr__(self, "session_id", session_id)
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise EvalValidationError("Unsupported TraceBundle schema version")
        if self.redacted is not True:
            raise EvalValidationError("TraceBundle must be explicitly marked redacted")
        if (
            not isinstance(self.source_sha256, str)
            or len(self.source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_sha256)
        ):
            raise EvalValidationError("source_sha256 must be lowercase SHA-256")
        canonical_events: list[object] = []
        redactor = Redactor()
        for event in self.events:
            if not isinstance(event, Mapping):
                raise EvalValidationError("TraceBundle events must be JSON objects")
            safe = redactor.redact(copy.deepcopy(dict(event)))
            if not isinstance(safe, dict):
                raise EvalValidationError("Redacted event must remain an object")
            if str(safe.get("session_id", "")) != session_id:
                raise EvalValidationError("TraceBundle event belongs to another session")
            canonical_events.append(_freeze(safe))
        object.__setattr__(self, "events", tuple(canonical_events))
        expected_hash = _exact_hash(canonical_events)
        if expected_hash != self.source_sha256:
            raise EvalValidationError("TraceBundle source hash does not match events")

    @classmethod
    def from_events(
        cls, events: Iterable[Event], *, redactor: Redactor | None = None
    ) -> TraceBundle:
        event_list = list(events)
        if not event_list:
            raise EvalValidationError("TraceBundle requires at least one event")
        session_id = _text(event_list[0].session_id, "session_id")
        safe_events: list[dict[str, object]] = []
        redact = redactor or Redactor()
        canonical_redactor = Redactor()
        for event in event_list:
            if event.session_id != session_id:
                raise EvalValidationError("TraceBundle events must share one session")
            safe = redact.redact(event.to_dict())
            if not isinstance(safe, dict):
                raise EvalValidationError("Redacted event must remain an object")
            normalized = canonical_redactor.redact(safe)
            if not isinstance(normalized, dict):
                raise EvalValidationError("Canonical redacted event must remain an object")
            safe_events.append(normalized)
        return cls(
            session_id=session_id,
            events=tuple(safe_events),
            source_sha256=_exact_hash(safe_events),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "redacted": self.redacted,
            "session_id": self.session_id,
            "source_sha256": self.source_sha256,
            "events": [_thaw(event) for event in self.events],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TraceBundle:
        if not isinstance(value, Mapping):
            raise EvalValidationError("TraceBundle must be a JSON object")
        raw_events = value.get("events")
        if not isinstance(raw_events, list):
            raise EvalValidationError("TraceBundle events must be a list")
        if any(not isinstance(event, dict) for event in raw_events):
            raise EvalValidationError("TraceBundle events must be JSON objects")
        raw_schema = value.get("schema_version", 1)
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise EvalValidationError("TraceBundle schema_version must be an integer")
        return cls(
            session_id=value.get("session_id"),  # type: ignore[arg-type]
            events=tuple(dict(event) for event in raw_events),
            source_sha256=value.get("source_sha256"),  # type: ignore[arg-type]
            schema_version=raw_schema,
            redacted=value.get("redacted") is True,
        )

    def domain_events(self) -> tuple[Event, ...]:
        try:
            return tuple(Event.from_dict(_thaw(event)) for event in self.events)  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise EvalValidationError("TraceBundle contains an invalid event") from exc


@dataclass(frozen=True, slots=True)
class ReplayResult:
    session_id: str
    event_count: int
    last_seq: int
    run_states: dict[str, str]
    message_count: int
    tool_call_count: int
    tool_result_count: int
    pending_tool_call_ids: tuple[str, ...]
    state_sha256: str
    event_type_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "event_count": self.event_count,
            "last_seq": self.last_seq,
            "run_states": dict(sorted(self.run_states.items())),
            "message_count": self.message_count,
            "tool_call_count": self.tool_call_count,
            "tool_result_count": self.tool_result_count,
            "pending_tool_call_ids": list(self.pending_tool_call_ids),
            "state_sha256": self.state_sha256,
            "event_type_counts": dict(sorted(self.event_type_counts.items())),
        }


@dataclass(slots=True)
class _ReplayState:
    session_id: str
    events: list[Event] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    last_seq: int = 0
    runs: dict[str, RunStateMachine] = field(default_factory=dict)
    tool_started: set[str] = field(default_factory=set)
    tool_completed: set[str] = field(default_factory=set)
    tool_runs: dict[str, str] = field(default_factory=dict)
    terminal_runs: set[str] = field(default_factory=set)

    def apply(self, event: Event) -> None:
        if event.session_id != self.session_id:
            raise ReplayInvariantViolation("Replay event belongs to another session")
        if event.id in self.event_ids:
            raise ReplayInvariantViolation(f"Duplicate replay event id: {event.id}")
        if event.seq is None or event.seq != self.last_seq + 1:
            raise ReplayInvariantViolation(
                f"Replay sequence gap: expected {self.last_seq + 1}, found {event.seq}"
            )
        if event.ephemeral:
            raise ReplayInvariantViolation("Ephemeral events cannot be replayed as durable facts")
        self._apply_run(event)
        self._apply_tool(event)
        self.event_ids.add(event.id)
        self.events.append(event)
        self.last_seq = event.seq

    def _apply_run(self, event: Event) -> None:
        if event.run_id is None:
            if event.type == "run.state_changed":
                raise ReplayInvariantViolation("Run state event is missing run_id")
            return
        if event.run_id in self.terminal_runs:
            raise ReplayInvariantViolation("Event occurred after run became terminal")
        if event.type == "run.started":
            if event.run_id in self.runs:
                raise ReplayInvariantViolation(f"Duplicate run.started: {event.run_id}")
            self.runs[event.run_id] = RunStateMachine()
            return
        if event.type == "run.state_changed":
            machine = self.runs.get(event.run_id)
            if machine is None:
                raise ReplayInvariantViolation("Run state changed before run.started")
            raw_state = event.data.get("state")
            try:
                target = RunState(str(raw_state))
                machine.transition(target)
            except (ValueError, InvalidRunTransition) as exc:
                raise ReplayInvariantViolation("Invalid replay run transition") from exc
            return
        if event.type in {"run.completed", "run.failed", "run.interrupted"}:
            machine = self.runs.get(event.run_id)
            if machine is None:
                raise ReplayInvariantViolation("Run terminal event before run.started")
            if event.run_id in self.terminal_runs:
                raise ReplayInvariantViolation("Duplicate run terminal event")
            target = {
                "run.completed": RunState.COMPLETED,
                "run.failed": RunState.FAILED,
                "run.interrupted": RunState.CANCELLED,
            }[event.type]
            raw_state = event.data.get("state")
            if raw_state is not None and str(raw_state) != target.value:
                raise ReplayInvariantViolation("Run terminal state does not match event type")
            if machine.state != target:
                try:
                    machine.transition(target)
                except InvalidRunTransition as exc:
                    raise ReplayInvariantViolation("Invalid replay terminal transition") from exc
            self.terminal_runs.add(event.run_id)

    def _apply_tool(self, event: Event) -> None:
        raw_call_id = event.data.get("tool_call_id")
        if event.type == "tool.started":
            if event.run_id is None:
                raise ReplayInvariantViolation("tool.started is missing run_id")
            call_id = _text(raw_call_id, "tool_call_id")
            if call_id in self.tool_started:
                raise ReplayInvariantViolation(f"Duplicate tool.started: {call_id}")
            self.tool_started.add(call_id)
            self.tool_runs[call_id] = event.run_id
        elif event.type == "tool.completed":
            if event.run_id is None:
                raise ReplayInvariantViolation("tool.completed is missing run_id")
            call_id = _text(raw_call_id, "tool_call_id")
            if call_id not in self.tool_started:
                raise ReplayInvariantViolation("tool.completed has no tool.started")
            if call_id in self.tool_completed:
                raise ReplayInvariantViolation(f"Duplicate tool.completed: {call_id}")
            if self.tool_runs[call_id] != event.run_id:
                raise ReplayInvariantViolation("Tool lifecycle crossed run boundary")
            self.tool_completed.add(call_id)
        elif event.type == "tool.result":
            raw_message = event.data.get("message")
            if not isinstance(raw_message, Mapping):
                return
            raw_content = raw_message.get("content", [])
            if not isinstance(raw_content, list):
                return
            for block in raw_content:
                if not isinstance(block, Mapping) or block.get("kind") != "tool_result":
                    continue
                call_id = _text(block.get("tool_call_id"), "tool_call_id")
                owner_run = self.tool_runs.get(call_id)
                if owner_run is not None and owner_run != event.run_id:
                    raise ReplayInvariantViolation("Tool result crossed run boundary")

    def result(self) -> ReplayResult:
        messages = _messages(self.events)
        calls = {call.id for message in messages for call in message.tool_calls}
        results = {
            result.tool_call_id for message in messages for result in message.tool_results
        }
        pending = tuple(sorted(calls - results))
        states = {run_id: machine.state.value for run_id, machine in self.runs.items()}
        event_type_counts: dict[str, int] = {}
        for event in self.events:
            event_type_counts[event.type] = event_type_counts.get(event.type, 0) + 1
        summary = {
            "session_id": self.session_id,
            "event_count": len(self.events),
            "last_seq": self.last_seq,
            "run_states": dict(sorted(states.items())),
            "message_count": len(messages),
            "tool_call_count": len(calls),
            "tool_result_count": len(results),
            "pending_tool_call_ids": list(pending),
            "event_type_counts": dict(sorted(event_type_counts.items())),
        }
        state_hash = hashlib.sha256(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ReplayResult(
            session_id=self.session_id,
            event_count=len(self.events),
            last_seq=self.last_seq,
            run_states=states,
            message_count=len(messages),
            tool_call_count=len(calls),
            tool_result_count=len(results),
            pending_tool_call_ids=pending,
            state_sha256=state_hash,
            event_type_counts=event_type_counts,
        )


def _messages(events: list[Event]) -> list[Message]:
    from aiharness.sessions.session import find_orphan_tool_calls, project_messages

    messages = project_messages(events)
    # A pending call is valid for interrupted/recoverable traces, but malformed
    # result references are not.  The projection performs that distinction.
    find_orphan_tool_calls(messages)
    return messages


class ReplayEngine:
    """Replays durable events into state summaries without executing side effects."""

    def replay(self, trace: TraceBundle | Iterable[Event]) -> ReplayResult:
        events = trace.domain_events() if isinstance(trace, TraceBundle) else tuple(trace)
        if not events:
            raise ReplayInvariantViolation("Replay requires at least one event")
        state = _ReplayState(session_id=_text(events[0].session_id, "session_id"))
        for event in events:
            state.apply(event)
        return state.result()
