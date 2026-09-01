"""Deterministic Event, Tool, and Artifact projection for rolling compaction."""

from __future__ import annotations

import json
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from aihi.agent._core.events import Event
from aihi.agent.artifacts import ArtifactRef
from aihi.agent.context.state import ArtifactState, ContextFact, ContextState
from aihi.agent.context.summary import StructuredSummary
from aihi.agent.tools.spec import ToolSpec
from aihi.models import Message, TextBlock, ToolCallBlock, ToolResultBlock, decode_message

_SEMANTIC_FIELDS = (
    ("constraints", "constraints"),
    ("decisions", "decisions"),
    ("open_questions", "open_questions"),
    ("next_steps", "next_steps"),
)


def project_context_state(
    *,
    messages: tuple[Message, ...] | list[Message],
    objective_messages: tuple[Message, ...] | list[Message] = (),
    events: tuple[Event, ...] | list[Event] = (),
    tools: tuple[ToolSpec, ...] = (),
    artifacts: tuple[ArtifactRef, ...] = (),
    previous: ContextState | None = None,
    enrichment: StructuredSummary | None = None,
    enrichment_source_message_ids: tuple[str, ...] = (),
    previous_compaction_id: str | None = None,
    omitted_message_count: int = 0,
    strategy: str = "rolling_summary",
) -> ContextState:
    """Build cumulative state without allowing model claims to become receipts.

    Files, verification, failures, approvals, subagents, and artifacts come only
    from durable events, tool-result metadata, artifact manifests, or an older
    evidence-backed ContextState. The optional model summary may enrich the
    semantic fields, but its ``files_changed`` and ``verified_state`` values are
    intentionally ignored.
    """

    prior = previous or ContextState()
    raw_messages, message_seqs = _event_messages(events)
    ordered_messages = _unique_messages((*raw_messages, *messages))
    specs = {tool.name: tool for tool in tools}
    calls, call_message_ids, results, result_message_ids = _tool_messages(ordered_messages)

    field_values: dict[str, dict[str, ContextFact]] = {
        name: {item.id: item for item in getattr(prior, name)}
        for name in (
            "constraints",
            "decisions",
            "files",
            "verified",
            "failures",
            "open_questions",
            "next_steps",
            "pending_approvals",
            "skills",
            "subagents",
        )
    }

    completed_call_ids: set[str] = set()
    for event in events:
        if event.type != "tool.completed":
            continue
        call_id = _string(event.data.get("tool_call_id"))
        tool_name = _string(event.data.get("tool_name"))
        if not call_id or not tool_name:
            continue
        completed_call_ids.add(call_id)
        raw_metadata = event.data.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        _project_tool_completion(
            fields=field_values,
            call_id=call_id,
            tool_name=tool_name,
            is_error=bool(event.data.get("is_error", False)),
            metadata=metadata,
            event_seq=event.seq,
            call=calls.get(call_id),
            call_message_id=call_message_ids.get(call_id),
            result=results.get(call_id),
            result_message_id=result_message_ids.get(call_id),
            message_seqs=message_seqs,
            spec=specs.get(tool_name),
        )

    # Direct compiler callers may not have an EventStore. Tool Result metadata
    # remains deterministic evidence, but it never gains an invented Event seq.
    for call_id, result in results.items():
        if call_id in completed_call_ids:
            continue
        call = calls.get(call_id)
        if call is None:
            continue
        _project_tool_completion(
            fields=field_values,
            call_id=call_id,
            tool_name=call.name,
            is_error=result.is_error,
            metadata=dict(result.metadata),
            event_seq=message_seqs.get(result_message_ids.get(call_id, "")),
            call=call,
            call_message_id=call_message_ids.get(call_id),
            result=result,
            result_message_id=result_message_ids.get(call_id),
            message_seqs=message_seqs,
            spec=specs.get(call.name),
        )

    _project_approvals(field_values["pending_approvals"], events)
    _project_subagents(field_values["subagents"], events)

    enrichment_sources = _evidence_boundaries(enrichment_source_message_ids)
    if enrichment is not None and enrichment_sources:
        for state_field, summary_field in _SEMANTIC_FIELDS:
            values = getattr(enrichment, summary_field)
            for text in values:
                fact = _fact(
                    state_field,
                    text,
                    reason="model_enrichment",
                    source_message_ids=enrichment_sources,
                    source_event_seqs=_seqs_for(enrichment_sources, message_seqs),
                )
                field_values[state_field][fact.id] = fact

    artifact_values = {item.artifact_id: item for item in prior.artifacts}
    _project_artifact_events(
        artifact_values,
        events,
        call_message_ids=call_message_ids,
        result_message_ids=result_message_ids,
        message_seqs=message_seqs,
    )
    deleted_artifact_ids = _deleted_artifact_ids(events)
    for artifact in artifacts:
        if artifact.artifact_id in deleted_artifact_ids:
            continue
        call_id = _string(artifact.metadata.get("tool_call_id"))
        source_ids = tuple(
            item
            for item in (
                _call_message_id(call_id, ordered_messages),
                result_message_ids.get(call_id),
            )
            if item
        )
        artifact_values[artifact.artifact_id] = ArtifactState(
            artifact_id=artifact.artifact_id,
            purpose=_string(artifact.metadata.get("purpose")) or "tool_result",
            sha256=artifact.sha256,
            scope=_artifact_scope(artifact),
            source_message_ids=source_ids,
            source_event_seqs=_seqs_for(source_ids, message_seqs),
        )

    objective, objective_source = _latest_user_objective(
        objective_messages or ordered_messages
    )
    if (
        not objective
        and enrichment is not None
        and enrichment.objective.strip()
        and enrichment_sources
    ):
        objective = enrichment.objective.strip()
        objective_source = enrichment_sources
    elif not objective:
        objective = prior.objective
        objective_source = prior.source_message_ids[:1]

    source_message_ids = list(prior.source_message_ids)
    source_event_seqs = list(prior.source_event_seqs)
    source_message_ids.extend(objective_source)
    source_event_seqs.extend(_seqs_for(objective_source, message_seqs))
    for values in field_values.values():
        for item in values.values():
            source_message_ids.extend(item.source_message_ids)
            source_event_seqs.extend(item.source_event_seqs)
    for artifact_state in artifact_values.values():
        source_message_ids.extend(artifact_state.source_message_ids)
        source_event_seqs.extend(artifact_state.source_event_seqs)

    return ContextState(
        strategy=strategy,
        objective=objective,
        artifacts=tuple(artifact_values.values()),
        source_message_ids=tuple(dict.fromkeys(source_message_ids)),
        source_event_seqs=tuple(dict.fromkeys(source_event_seqs)),
        previous_compaction_id=previous_compaction_id,
        event_cursor=max(
            (event.seq for event in events if event.seq is not None),
            default=prior.event_cursor,
        ),
        omitted_message_count=prior.omitted_message_count + omitted_message_count,
        constraints=_ordered_facts(field_values["constraints"]),
        decisions=_ordered_facts(field_values["decisions"]),
        files=_ordered_facts(field_values["files"]),
        verified=_ordered_facts(field_values["verified"]),
        failures=_ordered_facts(field_values["failures"]),
        open_questions=_ordered_facts(field_values["open_questions"]),
        next_steps=_ordered_facts(field_values["next_steps"]),
        pending_approvals=_ordered_facts(field_values["pending_approvals"]),
        skills=_ordered_facts(field_values["skills"]),
        subagents=_ordered_facts(field_values["subagents"]),
    )


def legacy_summary_state(message: Message) -> ContextState | None:
    """Upgrade a projected v1 summary in memory without rewriting its event."""

    try:
        raw = json.loads(message.text_content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or raw.get("kind") != "context_compaction_summary":
        return None
    source_ids = tuple(
        str(item)
        for item in message.metadata.get("source_message_ids", [])
        if isinstance(item, str)
    )
    fields: dict[str, tuple[ContextFact, ...]] = {}
    for target, source in (
        ("constraints", "constraints"),
        ("decisions", "decisions"),
        ("files", "files_changed"),
        ("verified", "verified_state"),
        ("open_questions", "open_questions"),
        ("next_steps", "next_steps"),
        ("skills", "skills"),
        ("subagents", "subagents"),
    ):
        raw_values = raw.get(source, [])
        values = raw_values if isinstance(raw_values, list) else []
        fields[target] = tuple(
            _fact(
                target,
                text,
                reason="legacy_summary",
                source_message_ids=source_ids,
            )
            for value in values
            if (text := _string(value))
        )
    omitted = raw.get("omitted_message_count", 0)
    return ContextState(
        strategy="rolling_summary_legacy",
        objective=_string(raw.get("objective")),
        source_message_ids=source_ids,
        omitted_message_count=(
            omitted if isinstance(omitted, int) and not isinstance(omitted, bool) else 0
        ),
        constraints=fields["constraints"],
        decisions=fields["decisions"],
        files=fields["files"],
        verified=fields["verified"],
        open_questions=fields["open_questions"],
        next_steps=fields["next_steps"],
        skills=fields["skills"],
        subagents=fields["subagents"],
    )


def _project_tool_completion(
    *,
    fields: dict[str, dict[str, ContextFact]],
    call_id: str,
    tool_name: str,
    is_error: bool,
    metadata: dict[str, Any],
    event_seq: int | None,
    call: ToolCallBlock | None,
    call_message_id: str | None,
    result: ToolResultBlock | None,
    result_message_id: str | None,
    message_seqs: dict[str, int],
    spec: ToolSpec | None,
) -> None:
    source_ids = tuple(
        item
        for item in (
            call_message_id,
            result_message_id,
        )
        if item
    )
    source_seqs = tuple(
        dict.fromkeys(
            item
            for item in (
                event_seq,
                *(_seqs_for(source_ids, message_seqs)),
            )
            if item is not None
        )
    )
    if is_error or (result is not None and result.is_error):
        error_code = _string(metadata.get("error_code")) or "tool_error"
        text = f"Tool {tool_name} failed ({error_code})"
        if result is not None and result.content.strip():
            text += f": {result.content.strip()[:512]}"
        fact = _fact(
            "failures",
            text,
            stable_key=call_id,
            reason="tool_result",
            source_message_ids=source_ids,
            source_event_seqs=source_seqs,
        )
        fields["failures"][fact.id] = fact
        return

    if spec is not None and spec.mutates:
        path = _string(metadata.get("path")) or (
            _string(call.input.get("path")) if call is not None else ""
        )
        if path:
            digest = (
                _string(metadata.get("sha256"))
                or _string(metadata.get("new_sha256"))
            )
            text = f"{path} updated by {tool_name}"
            if digest:
                text += f"; sha256={digest}"
            fact = _fact(
                "files",
                text,
                stable_key=path,
                reason="mutating_tool_receipt",
                source_message_ids=source_ids,
                source_event_seqs=source_seqs,
            )
            fields["files"][fact.id] = fact

    exit_code = metadata.get("exit_code")
    verification = _string(metadata.get("verification"))
    verified = metadata.get("verified") is True
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code == 0
    ) or verification or verified:
        command = _string(metadata.get("command")) or (
            _string(call.input.get("command")) if call is not None else ""
        )
        detail = verification or command or tool_name
        fact = _fact(
            "verified",
            f"Tool {tool_name} succeeded: {detail}",
            stable_key=call_id,
            reason="tool_verification_receipt",
            source_message_ids=source_ids,
            source_event_seqs=source_seqs,
        )
        fields["verified"][fact.id] = fact


def _project_approvals(values: dict[str, ContextFact], events: Iterable[Event]) -> None:
    for event in events:
        if event.type == "approval.requested":
            raw = event.data.get("approval")
            approval = dict(raw) if isinstance(raw, dict) else {}
            approval_id = _string(approval.get("approval_id"))
            if not approval_id:
                continue
            scope = _string(approval.get("scope")) or _string(event.data.get("tool_name"))
            call_id = _string(event.data.get("tool_call_id"))
            text = f"Approval {approval_id} pending for {scope or 'unknown scope'}"
            if call_id:
                text += f"; tool_call_id={call_id}"
            fact = _fact(
                "pending_approvals",
                text,
                stable_key=approval_id,
                reason="approval_event",
                source_event_seqs=(event.seq,) if event.seq is not None else (),
            )
            values[fact.id] = fact
        elif event.type in {"approval.resolved", "approval.consumed"}:
            approval_id = _string(event.data.get("approval_id"))
            if approval_id:
                values.pop(_fact_id("pending_approvals", approval_id), None)


def _project_subagents(values: dict[str, ContextFact], events: Iterable[Event]) -> None:
    for event in events:
        if event.type not in {"subagent.started", "subagent.completed"}:
            continue
        task_id = _string(event.data.get("task_id"))
        if not task_id:
            continue
        status = "started" if event.type.endswith("started") else "completed"
        objective = _string(event.data.get("objective"))
        text = f"Subagent {task_id} {status}"
        if objective:
            text += f": {objective}"
        fact = _fact(
            "subagents",
            text,
            stable_key=task_id,
            reason="subagent_event",
            source_event_seqs=(event.seq,) if event.seq is not None else (),
        )
        values[fact.id] = fact


def _project_artifact_events(
    values: dict[str, ArtifactState],
    events: Iterable[Event],
    *,
    call_message_ids: dict[str, str],
    result_message_ids: dict[str, str],
    message_seqs: dict[str, int],
) -> None:
    for event in events:
        if event.type == "artifact.deleted":
            raw_deleted = event.data.get("artifact")
            artifact_id = _string(event.data.get("artifact_id")) or (
                _string(raw_deleted.get("artifact_id"))
                if isinstance(raw_deleted, dict)
                else ""
            )
            if artifact_id:
                values.pop(artifact_id, None)
            continue
        if event.type != "artifact.created":
            continue
        raw = event.data.get("artifact")
        if not isinstance(raw, dict):
            continue
        try:
            artifact = ArtifactRef.from_dict(raw)
        except (TypeError, ValueError):
            continue
        call_id = _string(artifact.metadata.get("tool_call_id"))
        source_ids = tuple(
            item
            for item in (
                call_message_ids.get(call_id),
                result_message_ids.get(call_id),
            )
            if item
        )
        values[artifact.artifact_id] = ArtifactState(
            artifact_id=artifact.artifact_id,
            purpose=_string(event.data.get("purpose")) or "context",
            sha256=artifact.sha256,
            scope=_artifact_scope(artifact),
            source_message_ids=source_ids,
            source_event_seqs=tuple(
                dict.fromkeys(
                    (
                        *((event.seq,) if event.seq is not None else ()),
                        *_seqs_for(source_ids, message_seqs),
                    )
                )
            ),
        )


def _deleted_artifact_ids(events: Iterable[Event]) -> frozenset[str]:
    values: set[str] = set()
    for event in events:
        if event.type != "artifact.deleted":
            continue
        raw = event.data.get("artifact")
        artifact_id = _string(event.data.get("artifact_id")) or (
            _string(raw.get("artifact_id")) if isinstance(raw, dict) else ""
        )
        if artifact_id:
            values.add(artifact_id)
    return frozenset(values)


def _event_messages(events: Iterable[Event]) -> tuple[tuple[Message, ...], dict[str, int]]:
    messages: list[Message] = []
    seqs: dict[str, int] = {}
    for event in events:
        raw = event.data.get("message")
        if not isinstance(raw, dict):
            continue
        try:
            message = decode_message(
                {
                    "message_schema_version": event.data.get("message_schema_version", 1),
                    "message": raw,
                }
            )
        except (TypeError, ValueError):
            continue
        messages.append(message)
        if event.seq is not None:
            seqs[message.id] = event.seq
    return tuple(messages), seqs


def _tool_messages(
    messages: Iterable[Message],
) -> tuple[
    dict[str, ToolCallBlock],
    dict[str, str],
    dict[str, ToolResultBlock],
    dict[str, str],
]:
    calls: dict[str, ToolCallBlock] = {}
    call_message_ids: dict[str, str] = {}
    results: dict[str, ToolResultBlock] = {}
    result_message_ids: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            calls[call.id] = call
            call_message_ids[call.id] = message.id
        for result in message.tool_results:
            results[result.tool_call_id] = result
            result_message_ids[result.tool_call_id] = message.id
    return calls, call_message_ids, results, result_message_ids


def _unique_messages(messages: Iterable[Message]) -> tuple[Message, ...]:
    result: dict[str, Message] = {}
    for message in messages:
        result[message.id] = message
    return tuple(result.values())


def _latest_user_objective(messages: Iterable[Message]) -> tuple[str, tuple[str, ...]]:
    for message in reversed(tuple(messages)):
        if message.role != "user" or message.tool_results:
            continue
        text = "\n".join(
            block.text.strip()
            for block in message.content
            if isinstance(block, TextBlock) and block.text.strip()
        )
        if text:
            return text, (message.id,)
    return "", ()


def _fact(
    field: str,
    text: str,
    *,
    stable_key: str | None = None,
    reason: str | None = None,
    source_message_ids: tuple[str, ...] = (),
    source_event_seqs: tuple[int, ...] = (),
) -> ContextFact:
    return ContextFact(
        id=_fact_id(field, stable_key or text),
        text=text.strip(),
        reason=reason,
        source_message_ids=tuple(dict.fromkeys(source_message_ids)),
        source_event_seqs=tuple(dict.fromkeys(source_event_seqs)),
        observed_seq=max(source_event_seqs) if source_event_seqs else None,
    )


def _ordered_facts(values: dict[str, ContextFact]) -> tuple[ContextFact, ...]:
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (
                item.observed_seq
                if item.observed_seq is not None
                else max(item.source_event_seqs, default=-1)
            ),
        )
    )


def _fact_id(field: str, key: str) -> str:
    digest = sha256(f"{field}\0{key.strip()}".encode()).hexdigest()[:20]
    return f"{field}-{digest}"


def _seqs_for(ids: Iterable[str], seqs: dict[str, int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(seqs[item] for item in ids if item in seqs))


def _evidence_boundaries(ids: Iterable[str]) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(ids))
    if len(ordered) <= 2:
        return ordered
    # The CompactionRecord owns the exhaustive replaced-message list. Facts use
    # bounded range endpoints so evidence remains traceable without copying a
    # potentially huge ID list into every semantic item.
    return (ordered[0], ordered[-1])


def _artifact_scope(artifact: ArtifactRef) -> str:
    if artifact.policy.run_id is not None:
        return "run"
    if artifact.policy.session_id is not None:
        return "session"
    return "global"


def _call_message_id(call_id: str, messages: Iterable[Message]) -> str | None:
    for message in messages:
        if any(call.id == call_id for call in message.tool_calls):
            return message.id
    return None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = ["legacy_summary_state", "project_context_state"]
