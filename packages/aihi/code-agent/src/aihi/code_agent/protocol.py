"""JSON-RPC wire contract for the local Code Worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, TypeAlias

if TYPE_CHECKING:
    from aihi.code_agent.worker import WorkerServer

JsonObject: TypeAlias = dict[str, Any]
JsonRpcId: TypeAlias = str | int

PROTOCOL_VERSION: Final = "0.1"
SERVER_NAME: Final = "aihi-code-agent"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
NOT_INITIALIZED: Final = -32001
SHUTTING_DOWN: Final = -32002

COMMAND_DESCRIPTORS: Final[tuple[JsonObject, ...]] = (
    {
        "name": "session.create",
        "aliases": [],
        "scope": "session",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "session.list",
        "aliases": [],
        "scope": "session",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "session.get",
        "aliases": [],
        "scope": "session",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "session.events",
        "aliases": [],
        "scope": "session",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "task.create",
        "aliases": [],
        "scope": "task",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "task.spawn",
        "aliases": [],
        "scope": "task",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "task.get",
        "aliases": [],
        "scope": "task",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "task.list",
        "aliases": [],
        "scope": "task",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "task.transition",
        "aliases": [],
        "scope": "task",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "run.start",
        "aliases": [],
        "scope": "run",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "run.resume",
        "aliases": [],
        "scope": "run",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "run.list",
        "aliases": [],
        "scope": "run",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "run.cancel",
        "aliases": [],
        "scope": "run",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "session.fork",
        "aliases": [],
        "scope": "session",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "config.get",
        "aliases": [],
        "scope": "config",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "approval.list",
        "aliases": [],
        "scope": "approval",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "approval.resolve",
        "aliases": [],
        "scope": "approval",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "skill.list",
        "aliases": [],
        "scope": "skill",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "skill.trust",
        "aliases": [],
        "scope": "skill",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "skill.untrust",
        "aliases": [],
        "scope": "skill",
        "execution": "worker",
        "mutates": True,
        "requires_approval": False,
    },
    {
        "name": "mcp.list",
        "aliases": [],
        "scope": "integration",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
    {
        "name": "tool.list",
        "aliases": [],
        "scope": "integration",
        "execution": "worker",
        "mutates": False,
        "requires_approval": False,
    },
)
_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    str(command["name"]) for command in COMMAND_DESCRIPTORS
)


class RpcValidationError(ValueError):
    """A JSON-RPC request does not satisfy the wire contract."""

    def __init__(self, message: str, *, code: int = INVALID_REQUEST) -> None:
        super().__init__(message)
        self.code = code


def _valid_id(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _request_id(value: object) -> JsonRpcId | None:
    return value if _valid_id(value) else None  # type: ignore[return-value]


def _error_response(
    request_id: JsonRpcId | None,
    code: int,
    message: str,
    data: object | None = None,
) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result_response(request_id: JsonRpcId, result: object) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _notification(method: str, params: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def __getattr__(name: str) -> Any:
    """Resolve the legacy WorkerServer import without coupling the wire contract to runtime code."""

    if name == "WorkerServer":
        from aihi.code_agent.worker import WorkerServer

        return WorkerServer
    raise AttributeError(name)


__all__ = [
    "COMMAND_DESCRIPTORS",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JsonObject",
    "JsonRpcId",
    "METHOD_NOT_FOUND",
    "NOT_INITIALIZED",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "RpcValidationError",
    "SERVER_NAME",
    "SHUTTING_DOWN",
    # Kept for source compatibility; resolved lazily by __getattr__.
    "WorkerServer",
]
