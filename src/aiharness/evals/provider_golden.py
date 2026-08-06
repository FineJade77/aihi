"""Deterministic Provider golden tasks.

Golden tasks exercise only the provider boundary.  They consume the
provider-neutral stream protocol, so a transcript can be checked without
importing an SDK or invoking tools, policies, or a sandbox.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from aiharness.core.types import (
    ImageBlock,
    Message,
    ModelRequest,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from aiharness.evals.errors import EvalValidationError
from aiharness.models.base import (
    BlockEnd,
    BlockStart,
    MessageEnd,
    MessageStart,
    Provider,
    StreamChunk,
    TextDelta,
    ThinkingDelta,
    ToolInputDelta,
)
from aiharness.observability.telemetry import Redactor, stable_payload_hash

_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_EPHEMERAL_METADATA_KEYS = {
    "id",
    "message_id",
    "request_id",
    "run_id",
    "session_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "tool_call_id",
}
_MAX_TRANSCRIPT_CHUNKS = 4096
_MAX_CHUNK_BYTES = 64 * 1024
_MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024


def _nonempty(value: object, name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalValidationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise EvalValidationError(f"{name} exceeds {max_length} characters")
    return result


def _strict_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise EvalValidationError(f"{name} must be an object")
    result = dict(value)
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvalValidationError(f"{name} must be strict JSON") from exc
    return result


def _validate_chunks(chunks: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    if len(chunks) > _MAX_TRANSCRIPT_CHUNKS:
        raise EvalValidationError("provider transcript has too many chunks")
    normalized: list[dict[str, object]] = []
    total_bytes = 0
    for chunk in chunks:
        safe_chunk = _strict_object(chunk, "transcript chunk")
        encoded = json.dumps(safe_chunk, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > _MAX_CHUNK_BYTES:
            raise EvalValidationError("provider transcript chunk is too large")
        total_bytes += len(encoded)
        if total_bytes > _MAX_TRANSCRIPT_BYTES:
            raise EvalValidationError("provider transcript is too large")
        normalized.append(safe_chunk)
    return tuple(normalized)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Drop correlation-only metadata so fixtures survive new runtime IDs."""

    return {
        str(key): item
        for key, item in value.items()
        if str(key).lower() not in _EPHEMERAL_METADATA_KEYS
    }


def _raise_invalid_chunk(value: object) -> dict[str, object]:
    raise EvalValidationError(f"transcript chunk must be an object, got {type(value).__name__}")


def _request_payload(request: ModelRequest) -> dict[str, object]:
    """Return a provider-neutral request payload suitable for hashing.

    The payload is never emitted in a report.  ``stable_payload_hash`` applies
    the same bounded redaction policy used by observability before hashing.
    """

    return {
        "model": request.model,
        "system_prompt": request.system_prompt,
        "messages": [
            {
                "role": message.role,
                "content": [_normalize_block(block) for block in message.content],
                "metadata": _canonical_metadata(message.metadata),
            }
            for message in request.messages
        ],
        "tools": [tool.to_dict() for tool in request.tools],
        "max_output_tokens": request.max_output_tokens,
        "effort": request.effort,
        "metadata": _canonical_metadata(request.metadata),
        "timeout_seconds": request.timeout_seconds,
    }


def request_fingerprint(request: ModelRequest) -> str:
    """Hash the redacted canonical request without retaining prompt contents."""

    return stable_payload_hash(_request_payload(request))


def _normalize_block(block: object) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"kind": "text", "text": block.text, "stable_prefix": block.stable_prefix}
    if isinstance(block, ThinkingBlock):
        return {
            "kind": "thinking",
            "text": block.text,
            "provider": block.provider,
            "opaque_hash": stable_payload_hash(block.opaque) if block.opaque is not None else None,
        }
    if isinstance(block, ToolCallBlock):
        # Provider-generated call IDs are intentionally omitted: they are
        # correlation handles, not model behavior, and differ between runs.
        return {"kind": "tool_call", "name": block.name, "input": dict(block.input)}
    if isinstance(block, ToolResultBlock):
        return {
            "kind": "tool_result",
            "content": block.content,
            "is_error": block.is_error,
            "metadata": dict(block.metadata),
        }
    if isinstance(block, ImageBlock):
        return {
            "kind": "image",
            "media_type": block.media_type,
            "data_hash": hashlib.sha256(block.data.encode()).hexdigest(),
            "source_path": block.source_path,
        }
    raise EvalValidationError(f"unsupported provider response block: {type(block).__name__}")


def _normalize_message(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": [_normalize_block(block) for block in message.content],
    }


def normalize_chunk(chunk: StreamChunk) -> dict[str, object]:
    """Convert a normalized stream chunk to a strict, ID-stable JSON object."""

    if isinstance(chunk, MessageStart):
        normalized = {"kind": chunk.kind, "model": chunk.model}
    elif isinstance(chunk, BlockStart):
        normalized = {"kind": chunk.kind, "index": chunk.index, "block_kind": chunk.block_kind}
    elif isinstance(chunk, TextDelta):
        normalized = {"kind": chunk.kind, "index": chunk.index, "text": chunk.text}
    elif isinstance(chunk, ThinkingDelta):
        normalized = {"kind": chunk.kind, "index": chunk.index, "text": chunk.text}
    elif isinstance(chunk, ToolInputDelta):
        normalized = {"kind": chunk.kind, "index": chunk.index, "partial_json": chunk.partial_json}
    elif isinstance(chunk, BlockEnd):
        normalized = {"kind": chunk.kind, "index": chunk.index}
    elif isinstance(chunk, MessageEnd):
        normalized = {
            "kind": chunk.kind,
            "response": {
                "stop_reason": chunk.response.stop_reason,
                "message": _normalize_message(chunk.response.message),
                "usage": chunk.response.usage.to_dict(),
            },
        }
    else:
        raise EvalValidationError(f"unsupported provider stream chunk: {type(chunk).__name__}")
    return _strict_object(normalized, "normalized stream chunk")


def _diff(expected: object, actual: object, path: str = "") -> tuple[str, ...]:
    if expected == actual:
        return ()
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        keys = sorted(set(expected) | set(actual), key=str)
        differences: list[str] = []
        for key in keys:
            child = _diff(expected.get(key), actual.get(key), f"{path}.{key}")
            differences.extend(
                child
                or ((f"{path}.{key}",) if key not in expected or key not in actual else ())
            )
            if len(differences) >= 20:
                break
        return tuple(differences[:20])
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)) and isinstance(
        actual, Sequence
    ) and not isinstance(actual, (str, bytes)):
        differences = []
        if len(expected) != len(actual):
            differences.append(f"{path}.length")
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            differences.extend(_diff(left, right, f"{path}[{index}]"))
            if len(differences) >= 20:
                break
        return tuple(differences[:20])
    return (path or "$",)


@dataclass(frozen=True, slots=True)
class ProviderTranscript:
    provider_name: str
    model: str
    request_fingerprint: str
    chunks: tuple[dict[str, object], ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        provider_name = _nonempty(self.provider_name, "provider_name")
        model = _nonempty(self.model, "model")
        fingerprint = _nonempty(self.request_fingerprint, "request_fingerprint", max_length=64)
        if len(fingerprint) != 64 or fingerprint.lower() != fingerprint or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise EvalValidationError("request_fingerprint must be a lowercase SHA-256 hex digest")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise EvalValidationError("unsupported provider transcript schema_version")
        chunks = tuple(_freeze(chunk) for chunk in _validate_chunks(self.chunks))
        object.__setattr__(self, "provider_name", provider_name)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "request_fingerprint", fingerprint)
        object.__setattr__(self, "chunks", chunks)

    def to_dict(self, *, redactor: Redactor | None = None) -> dict[str, object]:
        redact = redactor or Redactor()
        return {
            "schema_version": self.schema_version,
            "provider_name": self.provider_name,
            "model": self.model,
            "request_fingerprint": self.request_fingerprint,
            "chunks": redact.redact(self.chunks),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProviderTranscript:
        if not isinstance(value, Mapping):
            raise EvalValidationError("provider transcript must be an object")
        raw_chunks = value.get("chunks", ())
        if not isinstance(raw_chunks, (list, tuple)):
            raise EvalValidationError("provider transcript chunks must be an array")
        schema_version = value.get("schema_version", 1)
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise EvalValidationError("schema_version must be an integer")
        return cls(
            provider_name=value.get("provider_name"),  # type: ignore[arg-type]
            model=value.get("model"),  # type: ignore[arg-type]
            request_fingerprint=value.get("request_fingerprint"),  # type: ignore[arg-type]
            chunks=tuple(
                dict(chunk)
                if isinstance(chunk, Mapping)
                else (_raise_invalid_chunk(chunk))
                for chunk in raw_chunks
            ),
            schema_version=schema_version,
        )


@dataclass(frozen=True, slots=True)
class ProviderGoldenTask:
    case_id: str
    provider_name: str
    request: ModelRequest
    expected: ProviderTranscript

    def __post_init__(self) -> None:
        case_id = _nonempty(self.case_id, "case_id")
        provider_name = _nonempty(self.provider_name, "provider_name")
        if not isinstance(self.request, ModelRequest) or not isinstance(
            self.expected, ProviderTranscript
        ):
            raise EvalValidationError("golden request and expected transcript types are invalid")
        if self.request.model != self.expected.model:
            raise EvalValidationError("golden request model differs from expected transcript")
        if provider_name != self.expected.provider_name:
            raise EvalValidationError("golden provider differs from expected transcript")
        if request_fingerprint(self.request) != self.expected.request_fingerprint:
            raise EvalValidationError("golden request fingerprint does not match request")
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "provider_name", provider_name)

    @classmethod
    def from_chunks(
        cls,
        case_id: str,
        provider_name: str,
        request: ModelRequest,
        chunks: Sequence[Mapping[str, object]],
    ) -> ProviderGoldenTask:
        expected = ProviderTranscript(
            provider_name=provider_name,
            model=request.model,
            request_fingerprint=request_fingerprint(request),
            chunks=tuple(dict(chunk) for chunk in chunks),
        )
        return cls(case_id, provider_name, request, expected)

    def to_dict(self) -> dict[str, object]:
        # Requests are deliberately not serialized: prompts and tool schemas
        # can contain sensitive content.  The fingerprint is sufficient for
        # checking that a fixture was paired with the intended request.
        return {
            "case_id": self.case_id,
            "provider_name": self.provider_name,
            "model": self.request.model,
            "request_fingerprint": self.expected.request_fingerprint,
            "expected": self.expected.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProviderGoldenResult:
    case_id: str
    passed: bool
    expected: ProviderTranscript | None = None
    actual: ProviderTranscript | None = None
    mismatch_paths: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _nonempty(self.case_id, "case_id"))
        if not isinstance(self.passed, bool):
            raise EvalValidationError("provider golden passed must be boolean")
        if len(self.mismatch_paths) > 20:
            raise EvalValidationError("provider golden mismatch_paths is too long")
        mismatch_paths = tuple(self.mismatch_paths)
        for path in mismatch_paths:
            _nonempty(path, "mismatch path", max_length=256)
        object.__setattr__(self, "mismatch_paths", mismatch_paths)
        for transcript in (self.expected, self.actual):
            if transcript is not None and not isinstance(transcript, ProviderTranscript):
                raise EvalValidationError("provider golden transcript type is invalid")
        if self.error_code is not None:
            code = _nonempty(self.error_code, "error_code", max_length=64).lower()
            if not _SAFE_CODE.fullmatch(code):
                raise EvalValidationError("error_code must be a stable machine-readable code")
            object.__setattr__(self, "error_code", code)
            if self.passed:
                raise EvalValidationError("a provider golden result with an error cannot pass")

    def to_dict(self, *, redactor: Redactor | None = None) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "expected": self.expected.to_dict(redactor=redactor) if self.expected else None,
            "actual": self.actual.to_dict(redactor=redactor) if self.actual else None,
            "mismatch_paths": list(self.mismatch_paths),
            "error_code": self.error_code,
        }


async def _collect(stream: AsyncIterator[StreamChunk]) -> list[dict[str, object]]:
    chunks: list[dict[str, object]] = []
    async for chunk in stream:
        chunks.append(normalize_chunk(chunk))
        if len(chunks) > _MAX_TRANSCRIPT_CHUNKS:
            raise EvalValidationError("provider transcript has too many chunks")
    return list(_validate_chunks(chunks))


class ProviderGoldenRunner:
    """Run one task against a provider and return a non-throwing verdict."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def run(self, task: ProviderGoldenTask) -> ProviderGoldenResult:
        provider_name = getattr(self.provider, "name", "")
        if provider_name != task.provider_name:
            return ProviderGoldenResult(
                task.case_id,
                False,
                expected=task.expected,
                error_code="provider_mismatch",
            )
        try:
            chunks = await _collect(self.provider.stream(task.request))
            actual = ProviderTranscript(
                provider_name=provider_name,
                model=task.request.model,
                request_fingerprint=request_fingerprint(task.request),
                chunks=tuple(chunks),
            )
            mismatch = _diff(task.expected.to_dict(), actual.to_dict())
            return ProviderGoldenResult(
                task.case_id,
                not mismatch,
                expected=task.expected,
                actual=actual,
                mismatch_paths=mismatch,
            )
        except EvalValidationError:
            return ProviderGoldenResult(
                task.case_id,
                False,
                expected=task.expected,
                error_code="invalid_provider_transcript",
            )
        except Exception as exc:  # provider errors are data in an offline gate
            code = getattr(exc, "code", "provider_stream_error")
            if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code.lower()):
                code = "provider_stream_error"
            return ProviderGoldenResult(
                task.case_id,
                False,
                expected=task.expected,
                error_code=code,
            )


async def run_provider_golden(provider: Provider, task: ProviderGoldenTask) -> ProviderGoldenResult:
    """Convenience wrapper for callers that do not need a runner object."""

    return await ProviderGoldenRunner(provider).run(task)


# Case is a useful spelling for callers that use EvalCase terminology.
ProviderGoldenCase = ProviderGoldenTask
