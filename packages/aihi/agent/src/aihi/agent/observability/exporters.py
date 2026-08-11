"""Optional telemetry exporters.

Both exporters accept canonical ``Observation`` values and apply the
redactor again at the boundary.  OpenTelemetry is intentionally optional;
the core package remains usable when the OTel API is not installed.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import TextIO

from aihi.agent.observability.telemetry import (
    Observation,
    Redactor,
    TelemetryError,
)


class ExporterUnavailable(TelemetryError):
    """Raised when an optional exporter dependency is unavailable."""

    code = "exporter_unavailable"


class JsonlTelemetrySink:
    """Write one strict, already-redacted observation per line."""

    def __init__(
        self,
        target: str | Path | TextIO,
        *,
        redactor: Redactor | None = None,
        flush: bool = True,
    ) -> None:
        if not isinstance(flush, bool):
            raise TelemetryError("flush must be boolean")
        self.redactor = redactor or Redactor()
        self.flush = flush
        self._lock = threading.RLock()
        self._owned = isinstance(target, str | Path)
        if isinstance(target, str | Path):
            path = Path(target).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._writer: TextIO = path.open("a", encoding="utf-8")
        else:
            if not hasattr(target, "write"):
                raise TelemetryError("JSONL target must be a path or writable text stream")
            self._writer = target

    def record(self, observation: Observation) -> None:
        if not isinstance(observation, Observation):
            raise TelemetryError("exporter accepts Observation values")
        payload = observation.to_dict(redactor=self.redactor)
        line = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        with self._lock:
            self._writer.write(line + "\n")
            if self.flush:
                self._writer.flush()

    def close(self) -> None:
        if self._owned:
            self._writer.close()

    def __enter__(self) -> JsonlTelemetrySink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return ("aiharness." + name)[:255]


def _attributes(value: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, raw in value.items():
        if isinstance(raw, (str, bool, int, float)) or raw is None:
            result[str(key)] = raw if raw is not None else ""
        else:
            result[str(key)] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return result


__all__ = ["ExporterUnavailable", "JsonlTelemetrySink"]
