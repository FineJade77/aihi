"""Tool execution contract and lightweight JSON-schema validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aiharness.core.errors import ToolInputError
from aiharness.core.types import ToolSpec
from aiharness.sandbox.base import SandboxBackend


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    cwd: str
    session_id: str
    run_id: str
    sandbox: SandboxBackend
    # The permission mode of the enclosing run. Tools that delegate work must
    # not hand a child more authority than the parent currently holds.
    permission_mode: str = "default"


class Tool(Protocol):
    spec: ToolSpec

    async def run(self, input: dict[str, Any], context: ToolContext) -> ToolResult: ...


def validate_tool_input(spec: ToolSpec, value: dict[str, Any]) -> None:
    """Validate the stable subset of JSON Schema used by built-in tools."""

    schema = spec.input_schema
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [str(name) for name in required if name not in value]
        if missing:
            raise ToolInputError(f"Missing required fields for {spec.name}: {', '.join(missing)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return
    python_types: dict[str, type[object] | tuple[type[object], ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, item in value.items():
        definition = properties.get(name)
        if not isinstance(definition, dict):
            if schema.get("additionalProperties") is False:
                raise ToolInputError(f"Unexpected field for {spec.name}: {name}")
            continue
        expected = python_types.get(str(definition.get("type")))
        if expected is not None and not isinstance(item, expected):
            raise ToolInputError(f"Field {name} for {spec.name} has the wrong type")
