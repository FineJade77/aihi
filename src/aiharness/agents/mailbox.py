"""Bounded, deterministic, structured messages between task graph nodes."""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from aiharness.core.events import utc_now
from aiharness.core.ids import new_id

from .errors import MailboxConflict, MailboxError
from .types import _json_object, _mapping, _text


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    sender_task_id: str
    recipient_task_id: str
    kind: str
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: new_id("msg"))
    correlation_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sender_task_id", _text(self.sender_task_id, "sender_task_id", max_length=256)
        )
        object.__setattr__(
            self,
            "recipient_task_id",
            _text(self.recipient_task_id, "recipient_task_id", max_length=256),
        )
        object.__setattr__(self, "kind", _text(self.kind, "kind", max_length=64))
        object.__setattr__(self, "message_id", _text(self.message_id, "message_id", max_length=256))
        if self.correlation_id is not None:
            object.__setattr__(
                self, "correlation_id", _text(self.correlation_id, "correlation_id", max_length=256)
            )
        if not isinstance(self.created_at, str) or not self.created_at:
            raise MailboxError("Mailbox created_at must be a non-empty string")
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))

    def to_dict(self) -> dict[str, object]:
        return {
            "sender_task_id": self.sender_task_id,
            "recipient_task_id": self.recipient_task_id,
            "kind": self.kind,
            "payload": deepcopy(self.payload),
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> MailboxMessage:
        if not isinstance(value, dict):
            raise MailboxError("Mailbox message must be an object")
        required = {"sender_task_id", "recipient_task_id", "kind", "payload", "message_id"}
        if not required.issubset(value) or any(
            not isinstance(value.get(key), str)
            for key in {"sender_task_id", "recipient_task_id", "kind", "message_id"}
        ):
            raise MailboxError("Mailbox message is missing or has invalid identity fields")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise MailboxError("Mailbox message payload must be an object")
        correlation_id = value.get("correlation_id")
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise MailboxError("Mailbox correlation_id must be a string or null")
        created_at = value.get("created_at", utc_now())
        if not isinstance(created_at, str):
            raise MailboxError("Mailbox created_at must be a string")
        return cls(
            sender_task_id=_text(value["sender_task_id"], "sender_task_id"),
            recipient_task_id=_text(value["recipient_task_id"], "recipient_task_id"),
            kind=_text(value["kind"], "mailbox kind"),
            payload=payload,
            message_id=_text(value["message_id"], "message_id"),
            correlation_id=correlation_id,
            created_at=created_at,
        )


class Mailbox:
    """FIFO mailbox with duplicate detection and explicit in-flight state.

    ``receive`` moves messages to an in-flight set.  A crash-safe caller can
    call ``requeue_inflight`` after restoring a snapshot, while normal callers
    call ``ack`` after their task has durably recorded the message.
    """

    def __init__(
        self,
        *,
        task_exists: Callable[[str], bool] | None = None,
        max_queue_size: int = 128,
        max_payload_bytes: int = 65_536,
    ) -> None:
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_queue_size <= 0
            or max_payload_bytes <= 0
        ):
            raise ValueError("Mailbox bounds must be positive")
        self._task_exists = task_exists
        self._max_queue_size = max_queue_size
        self._max_payload_bytes = max_payload_bytes
        self._queues: dict[str, deque[str]] = defaultdict(deque)
        self._messages: dict[str, MailboxMessage] = {}
        self._inflight: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._lock = threading.RLock()

    def register_task(self, task_id: str) -> None:
        _text(task_id, "task_id", max_length=256)
        with self._lock:
            self._queues.setdefault(task_id, deque())

    @staticmethod
    def _copy_message(message: MailboxMessage) -> MailboxMessage:
        return MailboxMessage.from_dict(message.to_dict())

    def send(self, message: MailboxMessage) -> None:
        encoded = json.dumps(message.payload, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > self._max_payload_bytes:
            raise MailboxError("Mailbox payload exceeds configured limit")
        with self._lock:
            if self._task_exists and (
                not self._task_exists(message.sender_task_id)
                or not self._task_exists(message.recipient_task_id)
            ):
                raise MailboxError("Mailbox sender and recipient must be known task IDs")
            if message.message_id in self._seen_ids:
                raise MailboxConflict(f"Duplicate mailbox message: {message.message_id}")
            queue = self._queues.setdefault(message.recipient_task_id, deque())
            if len(queue) >= self._max_queue_size:
                raise MailboxError("Mailbox queue is full")
            self._messages[message.message_id] = self._copy_message(message)
            self._seen_ids.add(message.message_id)
            queue.append(message.message_id)

    def receive(self, task_id: str, *, limit: int = 1) -> tuple[MailboxMessage, ...]:
        _text(task_id, "task_id", max_length=256)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise MailboxError("Mailbox receive limit must be positive")
        with self._lock:
            queue = self._queues.get(task_id)
            if queue is None:
                raise MailboxError(f"Unknown mailbox task: {task_id}")
            messages: list[MailboxMessage] = []
            while queue and len(messages) < limit:
                message_id = queue.popleft()
                message = self._messages[message_id]
                self._inflight[message_id] = task_id
                messages.append(self._copy_message(message))
            return tuple(messages)

    def ack(self, task_id: str, message_id: str) -> None:
        _text(task_id, "task_id", max_length=256)
        _text(message_id, "message_id", max_length=256)
        with self._lock:
            if self._inflight.get(message_id) != task_id:
                raise MailboxError("Mailbox message is not in-flight for this task")
            self._inflight.pop(message_id)
            self._messages.pop(message_id, None)

    def requeue_inflight(self, task_id: str | None = None) -> int:
        with self._lock:
            items = list(self._inflight.items())
            count = 0
            for message_id, recipient in items:
                if task_id is not None and recipient != task_id:
                    continue
                queue = self._queues[recipient]
                if len(queue) >= self._max_queue_size:
                    raise MailboxError("Mailbox queue is full while requeuing")
                queue.appendleft(message_id)
                self._inflight.pop(message_id)
                count += 1
            return count

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema_version": 1,
                "max_queue_size": self._max_queue_size,
                "max_payload_bytes": self._max_payload_bytes,
                "queues": {task_id: list(queue) for task_id, queue in sorted(self._queues.items())},
                "messages": {
                    message_id: message.to_dict()
                    for message_id, message in sorted(self._messages.items())
                },
                "inflight": dict(sorted(self._inflight.items())),
                "seen_ids": sorted(self._seen_ids),
            }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, object],
        *,
        task_exists: Callable[[str], bool] | None = None,
    ) -> Mailbox:
        if not isinstance(snapshot, dict):
            raise MailboxError("Mailbox snapshot must be an object")
        if snapshot.get("schema_version", 1) != 1:
            raise MailboxError("Unsupported mailbox snapshot schema")
        max_queue_size = snapshot.get("max_queue_size", 128)
        max_payload_bytes = snapshot.get("max_payload_bytes", 65_536)
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or isinstance(max_payload_bytes, bool)
            or not isinstance(max_payload_bytes, int)
            or max_queue_size <= 0
            or max_payload_bytes <= 0
        ):
            raise MailboxError("Mailbox snapshot bounds have invalid types")
        mailbox = cls(
            task_exists=task_exists,
            max_queue_size=max_queue_size,
            max_payload_bytes=max_payload_bytes,
        )
        raw_messages = _mapping(snapshot.get("messages", {}), "mailbox messages")
        raw_queues = _mapping(snapshot.get("queues", {}), "mailbox queues")
        raw_inflight = _mapping(snapshot.get("inflight", {}), "mailbox inflight")
        raw_seen = snapshot.get("seen_ids", [])
        if (
            not isinstance(raw_seen, list)
            or any(not isinstance(item, str) for item in raw_seen)
        ):
            raise MailboxError("Malformed mailbox snapshot")
        if any(not isinstance(key, str) for key in raw_messages):
            raise MailboxError("Mailbox message IDs must be strings")
        if any(not isinstance(key, str) for key in raw_queues):
            raise MailboxError("Mailbox queue IDs must be strings")
        if any(not isinstance(key, str) for key in raw_inflight) or any(
            not isinstance(value, str) for value in raw_inflight.values()
        ):
            raise MailboxError("Mailbox in-flight IDs must be strings")
        if not all(isinstance(value, dict) for value in raw_messages.values()):
            raise MailboxError("Malformed mailbox message")
        mailbox._messages = {
            key: MailboxMessage.from_dict(_mapping(value, "mailbox message"))
            for key, value in raw_messages.items()
        }
        if any(key != value.message_id for key, value in mailbox._messages.items()):
            raise MailboxError("Mailbox message key does not match message_id")
        mailbox._seen_ids = {str(item) for item in raw_seen}
        if set(mailbox._messages) - mailbox._seen_ids:
            raise MailboxError("Mailbox snapshot lost duplicate-detection state")
        mailbox._inflight = {key: str(value) for key, value in raw_inflight.items()}
        for task_id, raw_ids in raw_queues.items():
            if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
                raise MailboxError("Malformed mailbox queue")
            if len(raw_ids) > mailbox._max_queue_size:
                raise MailboxError("Mailbox snapshot queue exceeds configured limit")
            if task_exists and not task_exists(task_id):
                raise MailboxError("Mailbox snapshot references an unknown queue task")
            mailbox.register_task(task_id)
            mailbox._queues[task_id].extend(raw_ids)
            for message_id in raw_ids:
                message = mailbox._messages.get(message_id)
                if message is None or message.recipient_task_id != task_id:
                    raise MailboxError("Mailbox queue message recipient does not match queue")
        queued_ids = [message_id for queue in mailbox._queues.values() for message_id in queue]
        queued = set(queued_ids)
        if len(queued_ids) != len(queued):
            raise MailboxError("Mailbox snapshot contains duplicate queued messages")
        if (
            queued & set(mailbox._inflight)
            or not queued.issubset(mailbox._messages)
            or not set(mailbox._inflight).issubset(mailbox._messages)
        ):
            raise MailboxError("Mailbox snapshot has inconsistent message ownership")
        for message_id, recipient in mailbox._inflight.items():
            message = mailbox._messages[message_id]
            if recipient != message.recipient_task_id:
                raise MailboxError("Mailbox in-flight recipient does not match message")
            if task_exists and not task_exists(recipient):
                raise MailboxError("Mailbox snapshot references an unknown in-flight task")
        if queued | set(mailbox._inflight) != set(mailbox._messages):
            raise MailboxError("Mailbox snapshot contains orphan messages")
        for message in mailbox._messages.values():
            encoded = json.dumps(message.payload, allow_nan=False, separators=(",", ":")).encode()
            if len(encoded) > mailbox._max_payload_bytes:
                raise MailboxError("Mailbox snapshot payload exceeds configured limit")
        if task_exists and any(
            not task_exists(message.sender_task_id) or not task_exists(message.recipient_task_id)
            for message in mailbox._messages.values()
        ):
            raise MailboxError("Mailbox snapshot references an unknown task")
        return mailbox
