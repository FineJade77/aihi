"""Tool execution contract and lightweight JSON-schema validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from aihi.agent._core.errors import ToolInputError
from aihi.agent.tools.spec import ToolSpec

TAppContext = TypeVar("TAppContext")


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext(Generic[TAppContext]):
    cwd: str
    session_id: str
    run_id: str
    # The permission mode of the enclosing run. Tools that delegate work must
    # not hand a child more authority than the parent currently holds.
    permission_mode: str = "default"
    # Application-owned execution state. The Harness passes this through
    # without interpreting workspace, product mode, credentials or UI policy.
    # The legacy cwd/mode fields are removed once Coding tools migrate to this
    # explicit boundary.
    app_context: TAppContext | None = None


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """Validated, normalized input and non-secret execution metadata.

    Preparation must be deterministic and side-effect free. Policy and the
    eventual tool body consume the same normalized input, preventing policy
    from approving one path or target while execution interprets another.
    """

    input: dict[str, Any]
    execution: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol, Generic[TAppContext]):
    # Read-only on purpose: a plain class attribute satisfies this, and so does
    # a computed property (remote plugin/MCP tools derive their spec from the
    # server's advertised definition).
    @property
    def spec(self) -> ToolSpec: ...

    async def run(
        self, input: dict[str, Any], context: ToolContext[TAppContext]
    ) -> ToolExecutionResult: ...


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
        schema_type = str(definition.get("type"))
        bool_is_number = schema_type in {"integer", "number"} and isinstance(item, bool)
        if expected is not None and (bool_is_number or not isinstance(item, expected)):
            raise ToolInputError(f"Field {name} for {spec.name} has the wrong type")
