"""Deterministic secret scrubbing before memory persistence."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiharness.memory.errors import MemoryValidationError

_REDACTED = "[REDACTED_SECRET]"
_REDACTED_PII = "[REDACTED_PII]"


def _keep_secret_name(match: re.Match[str]) -> str:
    """Redact the value of a ``name = secret`` pair while keeping the name."""

    return f"{match.group(1)}{_REDACTED}"


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "credential_url",
        re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s]+", re.IGNORECASE),
    ),
    (
        "authorization",
        re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    ),
    (
        "named_secret",
        re.compile(
            r"(?i)(\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|token)"
            r"(?:\s*[:=]\s*|\s+is\s+|\s+))[\"']?[A-Za-z0-9._~+/=-]{1,}"
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("provider_token", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
)
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    (
        "phone",
        re.compile(
            r"(?<!\d)(?:1[3-9]\d{9}|\+?\d{1,3}[\s.-]?(?:\(?\d{3}\)?[\s.-])\d{3}[\s.-]\d{4})(?!\d)"
        ),
    ),
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "credential",
        "password",
        "passwd",
        "privatekey",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    redacted_count: int = 0
    categories: tuple[str, ...] = ()


class SecretRedactor:
    """Replace known credential forms without retaining the matched value."""

    def redact(self, value: str) -> RedactionResult:
        if not isinstance(value, str):
            raise MemoryValidationError("Only strings can be secret-scrubbed")
        text = value
        count = 0
        categories: list[str] = []
        for category, pattern in _SECRET_PATTERNS:
            replacement: str | Callable[[re.Match[str]], str] = (
                _keep_secret_name if category == "named_secret" else _REDACTED
            )
            text, replacements = pattern.subn(replacement, text)
            if replacements:
                count += replacements
                if category not in categories:
                    categories.append(category)
        for category, pattern in _PII_PATTERNS:
            text, replacements = pattern.subn(_REDACTED_PII, text)
            if replacements:
                count += replacements
                if category not in categories:
                    categories.append(category)
        return RedactionResult(text=text, redacted_count=count, categories=tuple(categories))

    def scrub_json(self, value: Any) -> Any:
        """Scrub metadata recursively while preserving JSON-shaped values."""

        if isinstance(value, str):
            return self.redact(value).text
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                result[str(key)] = (
                    _REDACTED
                    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS)
                    else self.scrub_json(item)
                )
            return result
        if isinstance(value, list):
            return [self.scrub_json(item) for item in value]
        if isinstance(value, tuple):
            return [self.scrub_json(item) for item in value]
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise MemoryValidationError("Memory metadata numbers must be finite")
            return value
        raise MemoryValidationError("Memory metadata must contain JSON-compatible values")


__all__ = ["RedactionResult", "SecretRedactor"]
