"""Governed, deterministic lifecycle Hook dispatch."""

from __future__ import annotations

import asyncio
import copy
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import Any

from aihi.agent.hooks.errors import (
    HookDispatchError,
    HookGovernanceError,
    HookRegistrationError,
)


class HookEventName(StrEnum):
    RUN_STARTED = "run.started"
    RUN_STOPPED = "run.stopped"
    BEFORE_MODEL = "model.before"
    AFTER_MODEL = "model.after"
    BEFORE_TOOL = "tool.before"
    AFTER_TOOL = "tool.after"
    POLICY_DECIDED = "policy.decided"
    BEFORE_COMPACT = "compact.before"
    AFTER_COMPACT = "compact.after"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"


class HookFailurePolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    CONTINUE = "continue"


HookHandler = Callable[["HookEvent"], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class HookGovernance:
    """Evidence supplied by the caller; a Hook cannot mint this evidence itself."""

    run_id: str | None
    policy_allowed: bool
    approval_id: str | None = None
    capability_lease_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.policy_allowed, bool):
            raise HookGovernanceError("Hook governance policy_allowed must be boolean")
    @property
    def allows_mutation(self) -> bool:
        return self.policy_allowed

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "policy_allowed": self.policy_allowed,
            "approval_id": self.approval_id,
            "capability_lease_ids": list(self.capability_lease_ids),
        }


@dataclass(frozen=True, slots=True)
class HookEvent:
    name: str
    payload: Mapping[str, Any]
    governance: HookGovernance | None = None

    def __post_init__(self) -> None:
        try:
            payload = copy.deepcopy(dict(self.payload))
        except Exception as exc:  # noqa: BLE001 - payload boundary must fail closed.
            raise HookRegistrationError("Hook payload must be copyable") from exc
        object.__setattr__(self, "payload", MappingProxyType(payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "payload": dict(self.payload),
            "governance": self.governance.to_dict() if self.governance else None,
        }


@dataclass(frozen=True, slots=True)
class HookRegistration:
    hook_id: str
    event_name: str
    handler: HookHandler
    priority: int
    sequence: int
    timeout_seconds: float
    failure_policy: HookFailurePolicy
    mutates: bool
    trusted: bool
    source: str


@dataclass(frozen=True, slots=True)
class HookOutcome:
    hook_id: str
    event_name: str
    success: bool
    elapsed_ms: float
    error_code: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "hook_id": self.hook_id,
            "event_name": self.event_name,
            "success": self.success,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class HookDispatch:
    event: HookEvent
    outcomes: tuple[HookOutcome, ...]

    @property
    def failures(self) -> tuple[HookOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.success)


class HookBus:
    """Dispatch handlers in priority order with explicit failure and governance rules."""

    def __init__(self, *, timeout_seconds: float = 5.0) -> None:
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Hook timeout must be positive")
        self._handlers: dict[str, list[HookRegistration]] = defaultdict(list)
        self._ids: set[str] = set()
        self._sequence = 0
        self.timeout_seconds = timeout_seconds

    def register(
        self,
        event_name: str | HookEventName,
        handler: HookHandler,
        *,
        hook_id: str | None = None,
        priority: int = 100,
        timeout_seconds: float | None = None,
        failure_policy: HookFailurePolicy = HookFailurePolicy.FAIL_FAST,
        mutates: bool = False,
        trusted: bool = False,
        source: str = "builtin",
    ) -> str:
        name = str(event_name)
        if not name or any(character.isspace() for character in name):
            raise HookRegistrationError("Hook event name must be non-empty and whitespace-free")
        if not callable(handler):
            raise HookRegistrationError("Hook handler must be callable")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise HookRegistrationError("Hook priority must be an integer")
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise HookRegistrationError("Hook timeout must be positive")
        try:
            policy = HookFailurePolicy(failure_policy)
        except ValueError as exc:
            raise HookRegistrationError("Unknown Hook failure policy") from exc
        if not isinstance(mutates, bool) or not isinstance(trusted, bool):
            raise HookRegistrationError("Hook mutates and trusted flags must be boolean")
        if mutates and not trusted:
            raise HookRegistrationError("Mutating Hooks require explicit trust")
        if not isinstance(source, str) or not source.strip():
            raise HookRegistrationError("Hook source must be explicit")
        self._sequence += 1
        identifier = hook_id or f"hook-{self._sequence}"
        if not isinstance(identifier, str) or not identifier.strip() or identifier in self._ids:
            raise HookRegistrationError(f"Duplicate or empty Hook id: {identifier!r}")
        registration = HookRegistration(
            hook_id=identifier,
            event_name=name,
            handler=handler,
            priority=priority,
            sequence=self._sequence,
            timeout_seconds=timeout,
            failure_policy=policy,
            mutates=mutates,
            trusted=trusted,
            source=source,
        )
        self._handlers[name].append(registration)
        self._ids.add(identifier)
        return identifier

    def unregister(self, hook_id: str) -> bool:
        removed = False
        for event_name, registrations in tuple(self._handlers.items()):
            remaining = [
                registration for registration in registrations if registration.hook_id != hook_id
            ]
            if len(remaining) != len(registrations):
                removed = True
                self._handlers[event_name] = remaining
        if removed:
            self._ids.remove(hook_id)
        return removed

    def registrations(self, event_name: str | HookEventName) -> tuple[HookRegistration, ...]:
        return tuple(
            sorted(
                self._handlers.get(str(event_name), ()),
                key=lambda registration: (registration.priority, registration.sequence),
            )
        )

    async def emit(
        self,
        event_name: str | HookEventName,
        payload: Mapping[str, Any],
        *,
        governance: HookGovernance | None = None,
    ) -> HookDispatch:
        event = HookEvent(str(event_name), payload, governance)
        registrations = self.registrations(event.name)
        if any(registration.mutates for registration in registrations):
            if governance is None or not governance.allows_mutation:
                raise HookGovernanceError(
                    f"Mutating Hooks require policy approval: {event.name}"
                )
        outcomes: list[HookOutcome] = []
        for registration in registrations:
            started = monotonic()
            handler_event = HookEvent(event.name, event.payload, event.governance)
            try:
                await asyncio.wait_for(
                    registration.handler(handler_event), timeout=registration.timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                outcome = HookOutcome(
                    registration.hook_id,
                    event.name,
                    False,
                    (monotonic() - started) * 1000,
                    "hook_timeout",
                    str(exc) or "Hook timed out",
                )
            except Exception as exc:  # noqa: BLE001 - failure policy handles Hook failures.
                outcome = HookOutcome(
                    registration.hook_id,
                    event.name,
                    False,
                    (monotonic() - started) * 1000,
                    getattr(exc, "code", "hook_failed"),
                    str(exc)[:500],
                )
            else:
                outcome = HookOutcome(
                    registration.hook_id,
                    event.name,
                    True,
                    (monotonic() - started) * 1000,
                )
            outcomes.append(outcome)
            if not outcome.success and registration.failure_policy == HookFailurePolicy.FAIL_FAST:
                raise HookDispatchError(
                    f"Hook failed during {event.name}: {registration.hook_id}",
                    details={
                        "event": event.name,
                        "outcomes": [item.to_dict() for item in outcomes],
                    },
                )
        return HookDispatch(event, tuple(outcomes))


__all__ = [
    "HookBus",
    "HookDispatch",
    "HookEvent",
    "HookEventName",
    "HookFailurePolicy",
    "HookGovernance",
    "HookHandler",
    "HookOutcome",
    "HookRegistration",
]
