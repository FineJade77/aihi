"""Event envelope versioning and the declared event catalogue.

`Event.schema_version` versions the **envelope** — the record shape every event
shares — not the payload of one event type. Payload changes are governed by the
compatibility rules below and covered by the Agent package's frozen-corpus
contract test.

Compatibility rules:

- Adding a new event type is additive and needs no version bump. Readers must
  ignore types they do not know.
- Adding an optional field to `data` is additive.
- Removing or renaming a field, or changing what an existing field means,
  requires a new envelope version plus a migration registered here.
- A reader must refuse an envelope version it does not understand rather than
  guess at the payload.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

#: The envelope version this harness writes.
EVENT_SCHEMA_VERSION = 3

#: Envelope versions this harness can read, oldest first.
SUPPORTED_EVENT_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2, 3)

#: Types the harness writes and persists. Every one of these must appear in the
#: frozen corpus contract test, so adding a durable type without a
#: compatibility fixture fails the build.
DURABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session.created",
        "session.repaired",
        "session.forked",
        "run.started",
        "run.resumed",
        "run.suspended",
        "run.state_changed",
        "run.completed",
        "run.failed",
        "run.interrupted",
        "run.cancelled",
        "user.message",
        "assistant.message",
        "model.usage",
        "system.message",
        "tool.result",
        "tool.requested",
        "tool.rejected",
        "tool.started",
        "tool.completed",
        "policy.decided",
        "approval.requested",
        "approval.resolved",
        "approval.consumed",
        "capability.lease.issued",
        "capability.lease.revoked",
        "compaction.created",
        "artifact.created",
        "artifact.deleted",
        "memory.candidate",
        "memory.written",
        "memory.deleted",
        "subagent.spawned",
        "subagent.started",
        "subagent.completed",
    }
)

#: Observer-only types. They reach `Session.emit` and never the store, so they
#: carry no compatibility obligation (ADR-0021).
EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset({"model.chunk"})

#: Types the projection still understands but nothing writes any more. They stay
#: readable so old sessions keep replaying; they are not part of the corpus.
LEGACY_EVENT_TYPES: frozenset[str] = frozenset({"message.added"})

KNOWN_EVENT_TYPES: frozenset[str] = (
    DURABLE_EVENT_TYPES | EPHEMERAL_EVENT_TYPES | LEGACY_EVENT_TYPES
)

#: Upgrades a raw event payload from one envelope version to the next.
EventUpgrade = Callable[[dict[str, Any]], dict[str, Any]]

def _upgrade_v1_to_v2(value: dict[str, Any]) -> dict[str, Any]:
    """Remove the retired Harness-owned workspace from subagent task payloads."""

    payload = deepcopy(value)
    if payload.get("type") != "subagent.spawned":
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    task = data.get("task")
    if isinstance(task, dict):
        task.pop("workspace", None)
    return payload


def _upgrade_v2_to_v3(value: dict[str, Any]) -> dict[str, Any]:
    """Remove Harness-owned product authority and global Sandbox evidence."""

    payload = deepcopy(value)
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    event_type = payload.get("type")
    if event_type in {"run.started", "run.resumed"}:
        for key in (
            "sandbox",
            "sandbox_descriptor",
            "workspace_root",
            "unsafe",
            "permission_mode",
        ):
            data.pop(key, None)
    elif event_type == "tool.started":
        for key in ("sandbox", "sandbox_descriptor", "unsafe"):
            data.pop(key, None)
    elif event_type == "approval.requested":
        data.pop("sandbox", None)
    elif event_type == "compaction.created":
        _remove_summary_permission_mode(data.get("summary"))
    return payload


def _remove_summary_permission_mode(value: object) -> None:
    if not isinstance(value, dict):
        return
    content = value.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            summary = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        if not isinstance(summary, dict) or summary.get("kind") != "context_compaction_summary":
            continue
        summary.pop("permission_mode", None)
        block["text"] = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


#: Registered upgrades keyed by the version they read.
EVENT_UPGRADES: Mapping[int, EventUpgrade] = {
    1: _upgrade_v1_to_v2,
    2: _upgrade_v2_to_v3,
}


class UnsupportedEventSchema(ValueError):
    """A stored event declares an envelope version this harness cannot read."""


def upgrade_event_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Bring a stored event payload up to `EVENT_SCHEMA_VERSION`.

    Fails closed: an unknown version is never read as if it were current.
    """

    raw = value.get("schema_version", EVENT_SCHEMA_VERSION)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise UnsupportedEventSchema(f"Event schema_version must be an integer, got {raw!r}")
    version = raw
    if version > EVENT_SCHEMA_VERSION or version < SUPPORTED_EVENT_SCHEMA_VERSIONS[0]:
        raise UnsupportedEventSchema(
            f"Unsupported event schema version {version}; "
            f"this harness reads {SUPPORTED_EVENT_SCHEMA_VERSIONS}"
        )
    payload = dict(value)
    payload["schema_version"] = version
    while version < EVENT_SCHEMA_VERSION:
        upgrade = EVENT_UPGRADES.get(version)
        if upgrade is None:
            raise UnsupportedEventSchema(f"No registered upgrade from event schema {version}")
        payload = upgrade(dict(payload))
        version += 1
        payload["schema_version"] = version
    return payload


__all__ = [
    "DURABLE_EVENT_TYPES",
    "EPHEMERAL_EVENT_TYPES",
    "EVENT_SCHEMA_VERSION",
    "EVENT_UPGRADES",
    "KNOWN_EVENT_TYPES",
    "LEGACY_EVENT_TYPES",
    "SUPPORTED_EVENT_SCHEMA_VERSIONS",
    "EventUpgrade",
    "UnsupportedEventSchema",
    "upgrade_event_payload",
]
