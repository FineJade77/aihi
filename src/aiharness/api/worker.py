"""Authenticated, transport-neutral Worker lease IPC adapter.

The adapter signs canonical JSON messages but does not open sockets, terminate
TLS, or grant permissions.  Hosts may carry the messages over HTTP, queues,
or Unix sockets and must still enforce their own TLS/mTLS policy.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from aiharness.core.errors import HarnessError
from aiharness.observability.telemetry import TelemetryError
from aiharness.observability.worker import (
    WorkerLeaseEnvelope,
    WorkerLeaseTraceBridge,
    WorkerLeaseTraceError,
)


class WorkerIpcAuthError(HarnessError):
    """The message signature or key identity is invalid."""

    code = "worker_ipc_auth_failed"


class WorkerLeaseIpcError(HarnessError):
    """The signed Worker lease message is structurally invalid."""

    code = "worker_ipc_request_invalid"


_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    if not isinstance(payload, Mapping):
        raise WorkerIpcAuthError("signed payload must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise WorkerIpcAuthError("signed payload keys must be strings")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise WorkerIpcAuthError("signed payload must be canonical JSON") from exc
    try:
        return encoded.encode("utf-8")
    except UnicodeError as exc:
        raise WorkerIpcAuthError("signed payload contains invalid Unicode") from exc


def _key_id(value: object) -> str:
    if not isinstance(value, str) or _KEY_ID.fullmatch(value) is None:
        raise WorkerIpcAuthError("key_id is invalid")
    return value


def _ttl(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerLeaseIpcError("ttl_seconds must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise WorkerLeaseIpcError("ttl_seconds must be a finite number") from exc
    if not math.isfinite(result) or result <= 0 or result > 86_400:
        raise WorkerLeaseIpcError("ttl_seconds must be greater than zero and at most one day")
    return result


@dataclass(frozen=True, slots=True)
class WorkerIpcAuthenticator:
    """HMAC-SHA256 key-ring verifier for Worker IPC messages.

    The secret is intentionally hidden from ``repr`` and never included in a
    signed response.  A key id enables explicit rotation without weakening
    constant-time MAC comparison.
    """

    keyring: Mapping[str, bytes] = field(repr=False)
    active_key_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.keyring, Mapping) or not self.keyring:
            raise WorkerIpcAuthError("keyring must contain at least one key")
        active = _key_id(self.active_key_id)
        normalized: dict[str, bytes] = {}
        for raw_id, secret in self.keyring.items():
            key_id = _key_id(raw_id)
            if not isinstance(secret, bytes) or len(secret) < 32:
                raise WorkerIpcAuthError("HMAC keys must be at least 32 bytes")
            normalized[key_id] = bytes(secret)
        if active not in normalized:
            raise WorkerIpcAuthError("active key_id is not present in keyring")
        object.__setattr__(self, "keyring", normalized)
        object.__setattr__(self, "active_key_id", active)

    def sign(self, payload: Mapping[str, object], *, key_id: str | None = None) -> str:
        selected = _key_id(self.active_key_id if key_id is None else key_id)
        try:
            secret = self.keyring[selected]
        except KeyError as exc:
            raise WorkerIpcAuthError("key_id is not configured") from exc
        digest = hmac.new(secret, _canonical_json(payload), hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"v1.{selected}.{encoded}"

    def verify(self, payload: Mapping[str, object], signature: object) -> None:
        if not isinstance(signature, str) or len(signature) > 256:
            raise WorkerIpcAuthError("signature is invalid")
        parts = signature.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise WorkerIpcAuthError("signature format is invalid")
        selected = _key_id(parts[1])
        try:
            secret = self.keyring[selected]
        except KeyError as exc:
            raise WorkerIpcAuthError("signature key_id is not configured") from exc
        encoded = parts[2]
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if not encoded or any(character not in alphabet for character in encoded):
            raise WorkerIpcAuthError("signature encoding is invalid")
        try:
            provided = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, binascii.Error) as exc:
            raise WorkerIpcAuthError("signature encoding is invalid") from exc
        expected = hmac.new(secret, _canonical_json(payload), hashlib.sha256).digest()
        if len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
            raise WorkerIpcAuthError("signature verification failed")


@dataclass(frozen=True, slots=True)
class SignedWorkerLease:
    """A response envelope plus its detached authentication signature."""

    envelope: WorkerLeaseEnvelope
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, WorkerLeaseEnvelope):
            raise WorkerLeaseIpcError("signed response requires WorkerLeaseEnvelope")
        if not isinstance(self.signature, str) or not self.signature:
            raise WorkerLeaseIpcError("signed response requires signature")

    def to_dict(self) -> dict[str, object]:
        return {"envelope": self.envelope.to_dict(), "signature": self.signature}


class WorkerLeaseIpcAdapter:
    """Authenticate messages before delegating to the fenced trace bridge."""

    def __init__(
        self,
        bridge: WorkerLeaseTraceBridge,
        authenticator: WorkerIpcAuthenticator,
    ) -> None:
        if not isinstance(bridge, WorkerLeaseTraceBridge):
            raise WorkerLeaseIpcError("bridge must be WorkerLeaseTraceBridge")
        if not isinstance(authenticator, WorkerIpcAuthenticator):
            raise WorkerLeaseIpcError("authenticator must be WorkerIpcAuthenticator")
        self.bridge = bridge
        self.authenticator = authenticator

    def sign_envelope(self, envelope: WorkerLeaseEnvelope) -> str:
        if not isinstance(envelope, WorkerLeaseEnvelope):
            raise WorkerLeaseIpcError("envelope is invalid")
        return self.authenticator.sign(envelope.to_dict())

    def _verify(self, payload: Mapping[str, object], signature: object) -> None:
        self.authenticator.verify(payload, signature)

    @staticmethod
    def _check_keys(payload: Mapping[str, object], allowed: set[str]) -> None:
        if not isinstance(payload, Mapping):
            raise WorkerLeaseIpcError("request body must be an object")
        unknown = set(payload) - allowed
        if unknown:
            raise WorkerLeaseIpcError("request body contains unknown fields")

    @staticmethod
    def _required_text(payload: Mapping[str, object], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise WorkerLeaseIpcError(f"{name} is required")
        return value.strip()

    @staticmethod
    def _parent_carrier(payload: Mapping[str, object]) -> Mapping[str, str] | None:
        value = payload.get("parent_carrier")
        if value is None:
            return None
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
        ):
            raise WorkerLeaseIpcError("parent_carrier must be a string mapping")
        return dict(value)

    def acquire(
        self, payload: Mapping[str, object], signature: object
    ) -> SignedWorkerLease:
        self._verify(payload, signature)
        self._check_keys(payload, {"run_id", "worker_id", "ttl_seconds", "parent_carrier"})
        run_id = self._required_text(payload, "run_id")
        worker_id = self._required_text(payload, "worker_id")
        ttl = _ttl(payload.get("ttl_seconds", 30.0))
        parent = self._parent_carrier(payload)
        try:
            envelope = self.bridge.acquire(
                run_id,
                worker_id,
                ttl_seconds=ttl,
                parent_carrier=parent,
            )
        except (TelemetryError, TypeError, ValueError) as exc:
            raise WorkerLeaseIpcError("worker lease acquire request is invalid") from exc
        return SignedWorkerLease(envelope, self.sign_envelope(envelope))

    def _envelope_request(
        self, payload: Mapping[str, object], signature: object
    ) -> tuple[WorkerLeaseEnvelope, float, Mapping[str, str] | None]:
        self._verify(payload, signature)
        self._check_keys(payload, {"envelope", "ttl_seconds", "parent_carrier"})
        raw = payload.get("envelope")
        if not isinstance(raw, Mapping):
            raise WorkerLeaseIpcError("envelope is required")
        try:
            envelope = WorkerLeaseEnvelope.from_dict(raw)
        except WorkerLeaseTraceError as exc:
            raise WorkerLeaseIpcError("envelope is invalid") from exc
        return envelope, _ttl(payload.get("ttl_seconds", 30.0)), self._parent_carrier(payload)

    def renew(
        self, payload: Mapping[str, object], signature: object
    ) -> SignedWorkerLease:
        envelope, ttl, parent = self._envelope_request(payload, signature)
        try:
            renewed = self.bridge.renew(
                envelope,
                ttl_seconds=ttl,
                parent_carrier=parent,
            )
        except (TelemetryError, TypeError, ValueError) as exc:
            raise WorkerLeaseIpcError("worker lease renew request is invalid") from exc
        return SignedWorkerLease(renewed, self.sign_envelope(renewed))

    def release(self, payload: Mapping[str, object], signature: object) -> None:
        self._verify(payload, signature)
        self._check_keys(payload, {"envelope"})
        raw = payload.get("envelope")
        if not isinstance(raw, Mapping):
            raise WorkerLeaseIpcError("envelope is required")
        try:
            envelope = WorkerLeaseEnvelope.from_dict(raw)
            self.bridge.release(envelope)
        except WorkerLeaseTraceError as exc:
            raise WorkerLeaseIpcError("envelope is invalid") from exc
