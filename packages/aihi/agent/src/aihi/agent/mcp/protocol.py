"""Provider-neutral MCP tool and JSON-RPC value contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aihi.agent.mcp.errors import McpProtocolError
from aihi.agent.tools.spec import ToolSpec
from aihi.models import JsonObject

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class McpToolAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None

    def __post_init__(self) -> None:
        if any(
            value is not None and not isinstance(value, bool)
            for value in (
                self.read_only_hint,
                self.destructive_hint,
                self.idempotent_hint,
                self.open_world_hint,
            )
        ):
            raise McpProtocolError("MCP tool annotation hints must be boolean")

    @classmethod
    def from_dict(cls, value: object) -> McpToolAnnotations:
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise McpProtocolError("MCP tool annotations must be an object")
        fields = {
            "readOnlyHint": "read_only_hint",
            "destructiveHint": "destructive_hint",
            "idempotentHint": "idempotent_hint",
            "openWorldHint": "open_world_hint",
        }
        parsed: dict[str, bool | None] = {}
        for wire_name, field_name in fields.items():
            raw = value.get(wire_name)
            if raw is not None and not isinstance(raw, bool):
                raise McpProtocolError(f"MCP annotation {wire_name} must be boolean")
            parsed[field_name] = raw
        return cls(**parsed)

    def to_dict(self) -> dict[str, bool]:
        values = {
            "readOnlyHint": self.read_only_hint,
            "destructiveHint": self.destructive_hint,
            "idempotentHint": self.idempotent_hint,
            "openWorldHint": self.open_world_hint,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    name: str
    description: str
    input_schema: JsonObject
    annotations: McpToolAnnotations = field(default_factory=McpToolAnnotations)
    required_capabilities: tuple[str, ...] = ()
    output_schema: JsonObject | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.description, str):
            raise McpProtocolError("MCP tool name and description must be strings")
        if not isinstance(self.annotations, McpToolAnnotations):
            raise McpProtocolError("MCP tool annotations must use McpToolAnnotations")
        if _TOOL_NAME.fullmatch(self.name) is None:
            raise McpProtocolError(f"Invalid MCP tool name: {self.name!r}")
        if not self.description.strip() or len(self.description) > 4_000:
            raise McpProtocolError("MCP tool description must be non-empty and bounded")
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise McpProtocolError("MCP inputSchema must be an object JSON Schema")
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise McpProtocolError("MCP outputSchema must be an object")
        if (
            self.annotations.read_only_hint is True
            and self.annotations.destructive_hint is True
        ):
            raise McpProtocolError("MCP readOnlyHint and destructiveHint cannot both be true")
        if any(not isinstance(item, str) or not item for item in self.required_capabilities):
            raise McpProtocolError("MCP required capabilities must be non-empty strings")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise McpProtocolError("MCP required capabilities must be unique")

    @classmethod
    def from_dict(cls, value: object) -> McpToolDefinition:
        if not isinstance(value, dict):
            raise McpProtocolError("MCP tool definition must be an object")
        name = value.get("name")
        description = value.get("description", "")
        input_schema = value.get("inputSchema")
        if not isinstance(name, str) or not isinstance(description, str):
            raise McpProtocolError("MCP tool name and description must be strings")
        if not isinstance(input_schema, dict):
            raise McpProtocolError("MCP tool inputSchema must be an object")
        raw_capabilities = value.get("x-aiharness-required-capabilities", [])
        if not isinstance(raw_capabilities, list | tuple) or any(
            not isinstance(item, str) for item in raw_capabilities
        ):
            raise McpProtocolError("MCP required capabilities must be an array of strings")
        output_schema = value.get("outputSchema")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise McpProtocolError("MCP outputSchema must be an object")
        return cls(
            name=name,
            description=description,
            input_schema=input_schema,
            annotations=McpToolAnnotations.from_dict(value.get("annotations")),
            required_capabilities=tuple(raw_capabilities),
            output_schema=output_schema,
        )

    @property
    def mutates(self) -> bool:
        return self.annotations.read_only_hint is not True

    @property
    def idempotent(self) -> bool:
        return self.annotations.idempotent_hint is True

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations.to_dict(),
        }
        if self.output_schema is not None:
            value["outputSchema"] = self.output_schema
        if self.required_capabilities:
            value["x-aiharness-required-capabilities"] = list(self.required_capabilities)
        return value

    def to_tool_spec(self, *, exposed_name: str | None = None) -> ToolSpec:
        return ToolSpec.define(
            name=exposed_name or self.name,
            description=self.description,
            input_schema=self.input_schema,
            concurrency_safe=self.idempotent,
            mutates=self.mutates,
            required_capabilities=self.required_capabilities,
        )


@dataclass(frozen=True, slots=True)
class McpCallResult:
    content: tuple[dict[str, Any], ...] = ()
    is_error: bool = False
    structured_content: JsonObject | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(item, dict) for item in self.content):
            raise McpProtocolError("MCP tool result content must contain objects")
        if not isinstance(self.is_error, bool):
            raise McpProtocolError("MCP tool result is_error must be boolean")
        if self.structured_content is not None and not isinstance(self.structured_content, dict):
            raise McpProtocolError("MCP structured content must be an object")

    @classmethod
    def from_dict(cls, value: object) -> McpCallResult:
        if not isinstance(value, dict):
            raise McpProtocolError("MCP tools/call result must be an object")
        raw_content = value.get("content", [])
        if not isinstance(raw_content, list) or any(
            not isinstance(item, dict) for item in raw_content
        ):
            raise McpProtocolError("MCP tool result content must be an array of objects")
        is_error = value.get("isError", False)
        if not isinstance(is_error, bool):
            raise McpProtocolError("MCP tool result isError must be boolean")
        structured = value.get("structuredContent")
        if structured is not None and not isinstance(structured, dict):
            raise McpProtocolError("MCP structuredContent must be an object")
        return cls(tuple(raw_content), is_error, structured)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"content": list(self.content), "isError": self.is_error}
        if self.structured_content is not None:
            value["structuredContent"] = self.structured_content
        return value


def jsonrpc_request(request_id: int, method: str, params: JsonObject | None = None) -> JsonObject:
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or not isinstance(method, str)
        or not method.strip()
        or params is not None
        and not isinstance(params, dict)
    ):
        raise McpProtocolError("JSON-RPC request requires integer id and method")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params if params is not None else {},
    }


def validate_jsonrpc_response(value: object, request_id: int) -> dict[str, Any]:
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise McpProtocolError("JSON-RPC request id must be an integer")
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise McpProtocolError("MCP response is not a matching JSON-RPC 2.0 response")
    response_id = value.get("id")
    if (
        isinstance(response_id, bool)
        or not isinstance(response_id, int | str)
        or response_id != request_id
    ):
        raise McpProtocolError("MCP response id does not match the request")
    has_result = "result" in value
    has_error = "error" in value
    if has_result == has_error:
        raise McpProtocolError("MCP response must contain exactly one result or error")
    if has_error:
        error = value["error"]
        if not isinstance(error, dict):
            raise McpProtocolError("MCP JSON-RPC error must be an object")
        code = error.get("code")
        message = error.get("message")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or not isinstance(message, str)
            or not message
        ):
            raise McpProtocolError("MCP JSON-RPC error requires integer code and message")
        if "data" in error and not isinstance(error["data"], dict):
            raise McpProtocolError("MCP JSON-RPC error data must be an object")
    return value


__all__ = [
    "McpCallResult",
    "McpToolAnnotations",
    "McpToolDefinition",
    "jsonrpc_request",
    "validate_jsonrpc_response",
]
