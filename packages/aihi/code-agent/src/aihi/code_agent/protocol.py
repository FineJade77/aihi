"""Small, dependency-free JSON-RPC contract for the local Code Worker."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Final, TypeAlias

from aihi.agent import (
    AgentBudget,
    AgentRuntimeError,
    AgentState,
    Event,
    EventStore,
    InMemoryEventStore,
    Session,
    SQLiteEventStore,
    TaskGraph,
    WorkspaceScope,
)

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


class WorkerServer:
    """Dispatch lifecycle, Session, and Task commands for one Worker process."""

    def __init__(
        self,
        *,
        store: EventStore | None = None,
        store_path: str | Path | None = None,
    ) -> None:
        if store is not None and store_path is not None:
            raise ValueError("store and store_path are mutually exclusive")
        self.initialized = False
        self.client_initialized = False
        self.shutdown_requested = False
        self._store: EventStore | None = store
        self._store_path = str(store_path) if store_path is not None else None
        self._owns_store = False
        self._sessions: dict[str, Session] = {}
        self._task_graphs: dict[str, TaskGraph] = {}
        self._pending_notifications: list[JsonObject] = []

    def handle(self, message: object) -> JsonObject | None:
        """Handle one decoded JSON value and return a response if required."""

        if not isinstance(message, Mapping):
            return _error_response(None, INVALID_REQUEST, "Request must be a JSON object")
        raw_id = message.get("id")
        request_id = _request_id(raw_id) if "id" in message else None
        is_notification = "id" not in message
        if "id" in message and not _valid_id(raw_id):
            return _error_response(None, INVALID_REQUEST, "Request id must be a string or integer")
        if message.get("jsonrpc") != "2.0":
            return self._maybe_error(
                is_notification,
                request_id,
                INVALID_REQUEST,
                "jsonrpc must be '2.0'",
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return self._maybe_error(
                is_notification, request_id, INVALID_REQUEST, "method must be a non-empty string"
            )
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            return self._maybe_error(
                is_notification, request_id, INVALID_PARAMS, "params must be a JSON object"
            )
        params_dict = dict(params)

        if method == "initialize":
            try:
                return self._initialize(request_id, params_dict, is_notification)
            except RpcValidationError as error:
                return self._maybe_error(is_notification, request_id, error.code, str(error))
            except (AgentRuntimeError, TypeError, ValueError) as error:
                return self._maybe_error(is_notification, request_id, INVALID_PARAMS, str(error))
        if method == "initialized":
            if not self.initialized:
                return self._maybe_error(
                    is_notification, request_id, NOT_INITIALIZED, "initialize is required first"
                )
            self.client_initialized = True
            return None
        if method == "shutdown":
            if not self.initialized:
                return self._maybe_error(
                    is_notification, request_id, NOT_INITIALIZED, "initialize is required first"
                )
            self.shutdown_requested = True
            return None if is_notification else _result_response(request_id, {"ok": True})  # type: ignore[arg-type]
        if not self.initialized:
            return self._maybe_error(
                is_notification, request_id, NOT_INITIALIZED, "initialize is required first"
            )
        if self.shutdown_requested:
            return self._maybe_error(
                is_notification, request_id, SHUTTING_DOWN, "worker is shutting down"
            )
        if method not in _COMMAND_NAMES:
            return self._maybe_error(
                is_notification,
                request_id,
                METHOD_NOT_FOUND,
                f"Method not found: {method}",
            )
        try:
            result = self._dispatch(method, params_dict)
        except RpcValidationError as error:
            return self._maybe_error(is_notification, request_id, error.code, str(error))
        except (AgentRuntimeError, TypeError, ValueError) as error:
            return self._maybe_error(is_notification, request_id, INVALID_PARAMS, str(error))
        return None if is_notification else _result_response(request_id, result)  # type: ignore[arg-type]

    def close(self) -> None:
        """Close an owned persistent store after the stdio loop stops."""

        if self._owns_store and self._store is not None:
            close = getattr(self._store, "close", None)
            if callable(close):
                close()
            self._owns_store = False

    def drain_notifications(self) -> list[JsonObject]:
        """Return queued event notifications in durable sequence order."""

        pending = self._pending_notifications
        self._pending_notifications = []
        return pending

    def _dispatch(self, method: str, params: JsonObject) -> JsonObject:
        if method == "session.create":
            return self._session_create(params)
        if method == "session.list":
            return self._session_list(params)
        if method == "session.get":
            return {"session": self._session_descriptor(self._load_session(params))}
        if method == "session.events":
            return self._session_events(params)
        if method == "task.create":
            return self._task_create(params)
        if method == "task.spawn":
            return self._task_spawn(params)
        if method == "task.get":
            session = self._load_session(params)
            graph = self._task_graph(session)
            task_id = self._required_text(params, "task_id")
            return {"task": graph.get(task_id).to_dict()}
        if method == "task.list":
            session = self._load_session(params)
            graph = self._task_graph(session)
            active_only = params.get("active_only", False)
            if not isinstance(active_only, bool):
                raise RpcValidationError("active_only must be a boolean", code=INVALID_PARAMS)
            if active_only:
                nodes = graph.active_tasks()
            else:
                snapshot = graph.snapshot()
                raw_nodes = snapshot.get("nodes", {})
                if not isinstance(raw_nodes, dict):
                    raise RpcValidationError(
                        "Task graph snapshot nodes must be an object", code=INTERNAL_ERROR
                    )
                nodes = tuple(graph.get(task_id) for task_id in sorted(raw_nodes))
            return {"session_id": session.id, "tasks": [node.to_dict() for node in nodes]}
        if method == "task.transition":
            return self._task_transition(params)
        raise RpcValidationError(f"Method not found: {method}", code=METHOD_NOT_FOUND)

    def _ensure_store(self) -> EventStore:
        if self._store is None:
            if self._store_path:
                self._store = SQLiteEventStore(self._store_path)
            else:
                self._store = InMemoryEventStore()
            self._owns_store = True
        return self._store

    def _load_session(self, params: Mapping[str, Any]) -> Session:
        session_id = self._required_text(params, "session_id")
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        session = Session.load(
            self._ensure_store(),
            session_id,
            event_observer=self._observe_event,
        )
        self._sessions[session_id] = session
        return session

    def _session_create(self, params: JsonObject) -> JsonObject:
        cwd = self._required_text(params, "cwd", max_length=4_096)
        provider = self._required_text(params, "provider", max_length=256)
        model = self._required_text(params, "model", max_length=256)
        session_id = params.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise RpcValidationError("session_id must be a non-empty string", code=INVALID_PARAMS)
        metadata = self._optional_mapping(params, "metadata")
        session = Session.create(
            self._ensure_store(),
            cwd=cwd,
            provider=provider,
            model=model,
            session_id=session_id,
            metadata=metadata,
            event_observer=self._observe_event,
        )
        self._sessions[session.id] = session
        return {"session": self._session_descriptor(session)}

    def _session_list(self, params: JsonObject) -> JsonObject:
        limit = self._limit(params.get("limit", 50), name="limit", maximum=100)
        sessions = self._ensure_store().list_sessions(limit=limit)
        return {
            "sessions": [
                {
                    "session_id": info.session_id,
                    "head_seq": info.head_seq,
                    "created_at": info.created_at,
                    "metadata": deepcopy(info.metadata),
                    "parent_session_id": info.parent_session_id,
                }
                for info in sessions
            ]
        }

    def _session_events(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        after_seq = self._non_negative_int(params.get("after_seq", 0), "after_seq")
        limit = self._limit(params.get("limit", 100), name="limit", maximum=500)
        events = self._ensure_store().read(session.id, after_seq=after_seq)
        selected = events[:limit]
        next_after_seq = after_seq
        if selected and selected[-1].seq is not None:
            next_after_seq = selected[-1].seq
        return {
            "session_id": session.id,
            "events": [event.to_dict() for event in selected],
            "head_seq": self._ensure_store().get(session.id).head_seq,
            "next_after_seq": next_after_seq,
            "has_more": len(events) > len(selected),
        }

    def _task_create(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        graph = self._task_graph(session)
        workspace = self._workspace(params.get("workspace"), default_root=str(session.cwd))
        budget = self._budget(params.get("budget"))
        node = graph.create_root(
            parent_run_id=self._required_text(params, "parent_run_id"),
            objective=self._required_text(params, "objective"),
            budget=budget,
            workspace=workspace,
            capabilities=self._string_collection(params.get("capabilities", []), "capabilities"),
            constraints=self._string_collection(params.get("constraints", []), "constraints"),
            max_depth=self._non_negative_int(params.get("max_depth", 4), "max_depth"),
            max_children=self._non_negative_int(
                params.get("max_children", 8), "max_children"
            ),
            metadata=self._optional_mapping(params, "metadata"),
        )
        return {"session_id": session.id, "task": node.to_dict()}

    def _task_spawn(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        graph = self._task_graph(session)
        parent = graph.get(self._required_text(params, "parent_task_id"))
        raw_budget = params.get("budget")
        budget = (
            self._budget(raw_budget, fallback=parent.spec.budget)
            if raw_budget is not None
            else None
        )
        raw_workspace = params.get("workspace")
        workspace = (
            self._workspace(raw_workspace, default_root=parent.spec.workspace.root)
            if raw_workspace is not None
            else None
        )
        raw_capabilities = params.get("capabilities")
        capabilities = (
            self._string_collection(raw_capabilities, "capabilities")
            if raw_capabilities is not None
            else None
        )
        node = graph.spawn(
            parent.spec.task_id,
            objective=self._required_text(params, "objective"),
            budget=budget,
            workspace=workspace,
            capabilities=capabilities,
            constraints=self._string_collection(params.get("constraints", []), "constraints"),
            metadata=self._optional_mapping(params, "metadata"),
        )
        return {"session_id": session.id, "task": node.to_dict()}

    def _task_transition(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        graph = self._task_graph(session)
        task_id = self._required_text(params, "task_id")
        state = self._required_text(params, "state", max_length=32).lower()
        reason = params.get("reason", "")
        if not isinstance(reason, str):
            raise RpcValidationError("reason must be a string", code=INVALID_PARAMS)
        if state == AgentState.RUNNING.value:
            node = graph.resume(task_id)
        elif state == AgentState.WAITING.value:
            node = graph.transition(task_id, AgentState.WAITING, reason=reason or None)
        elif state == AgentState.COMPLETED.value:
            node = graph.complete(
                task_id,
                summary=self._optional_text(params.get("summary", ""), "summary"),
                output_artifact_ids=self._string_collection(
                    params.get("output_artifact_ids", []), "output_artifact_ids"
                ),
                metrics=self._optional_mapping(params, "metrics"),
            )
        elif state == AgentState.FAILED.value:
            node = graph.fail(
                task_id,
                error=self._optional_text(params.get("error", reason), "error"),
            )
        elif state == AgentState.CANCELLED.value:
            node = graph.cancel(task_id, reason=reason or "cancelled")
        elif state == AgentState.INTERRUPTED.value:
            node = graph.interrupt(task_id, reason=reason or "interrupted")
        else:
            raise RpcValidationError(
                "state must be waiting, running, completed, failed, cancelled, or interrupted",
                code=INVALID_PARAMS,
            )
        return {"session_id": session.id, "task": node.to_dict()}

    def _task_graph(self, session: Session) -> TaskGraph:
        cached = self._task_graphs.get(session.id)
        if cached is not None:
            return cached
        nodes: dict[str, JsonObject] = {}
        for event in session.events:
            if not event.type.startswith("subagent."):
                continue
            raw_task = event.data.get("task")
            if not isinstance(raw_task, dict):
                continue
            task_id = raw_task.get("task_id")
            if isinstance(task_id, str):
                nodes[task_id] = {
                    "spec": deepcopy(raw_task),
                    "state": event.data.get("state", AgentState.PENDING.value),
                    "child_task_ids": [],
                    "result": deepcopy(event.data.get("result")),
                    "reason": event.data.get("reason"),
                    "updated_at": event.created_at,
                }
        if not nodes:
            graph = TaskGraph(session_id=session.id, event_sink=self._task_event_sink)
            self._task_graphs[session.id] = graph
            return graph
        children: dict[str, list[str]] = {task_id: [] for task_id in nodes}
        roots: list[str] = []
        for task_id, raw_node in nodes.items():
            spec = raw_node.get("spec")
            parent_id = spec.get("parent_task_id") if isinstance(spec, dict) else None
            if isinstance(parent_id, str) and parent_id in children:
                children[parent_id].append(task_id)
            elif parent_id is None:
                roots.append(task_id)
        for task_id, raw_node in nodes.items():
            raw_node["child_task_ids"] = children[task_id]
        if len(roots) != 1:
            raise RpcValidationError(
                "Persisted task graph must contain one root", code=INVALID_PARAMS
            )
        snapshot: dict[str, object] = {
            "schema_version": 1,
            "session_id": session.id,
            "roots": roots,
            "nodes": nodes,
        }
        graph = TaskGraph.from_snapshot(snapshot, event_sink=self._task_event_sink)
        self._task_graphs[session.id] = graph
        return graph

    def _task_event_sink(self, event: Event) -> None:
        session = self._sessions.get(event.session_id)
        if session is None:
            raise RpcValidationError(
                "Task event references an unloaded session", code=INTERNAL_ERROR
            )
        session.append(event)

    def _observe_event(self, event: Event) -> None:
        self._pending_notifications.append(
            self.emit_event(
                event.type,
                session_id=event.session_id,
                run_id=event.run_id,
                seq=event.seq,
                ephemeral=event.ephemeral,
                data=event.data,
            )
        )

    def _session_descriptor(self, session: Session) -> JsonObject:
        info = self._ensure_store().get(session.id)
        return {
            "session_id": info.session_id,
            "head_seq": info.head_seq,
            "created_at": info.created_at,
            "metadata": deepcopy(info.metadata),
            "parent_session_id": info.parent_session_id,
        }

    @staticmethod
    def _required_text(
        params: Mapping[str, Any], key: str, *, max_length: int = 4_096
    ) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RpcValidationError(f"{key} must be a non-empty string", code=INVALID_PARAMS)
        value = value.strip()
        if len(value) > max_length:
            raise RpcValidationError(f"{key} exceeds {max_length} characters", code=INVALID_PARAMS)
        return value

    @staticmethod
    def _optional_text(value: object, key: str, *, max_length: int = 4_096) -> str:
        if not isinstance(value, str):
            raise RpcValidationError(f"{key} must be a string", code=INVALID_PARAMS)
        if len(value) > max_length:
            raise RpcValidationError(f"{key} exceeds {max_length} characters", code=INVALID_PARAMS)
        return value

    @staticmethod
    def _optional_mapping(params: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = params.get(key, {})
        if not isinstance(value, Mapping):
            raise RpcValidationError(f"{key} must be a JSON object", code=INVALID_PARAMS)
        return dict(value)

    @staticmethod
    def _string_collection(value: object, key: str) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
            raise RpcValidationError(f"{key} must be a collection of strings", code=INVALID_PARAMS)
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise RpcValidationError(f"{key} must contain non-empty strings", code=INVALID_PARAMS)
        return tuple(item.strip() for item in value)

    @staticmethod
    def _non_negative_int(value: object, key: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RpcValidationError(f"{key} must be a non-negative integer", code=INVALID_PARAMS)
        return value

    @staticmethod
    def _limit(value: object, *, name: str, maximum: int) -> int:
        parsed = WorkerServer._non_negative_int(value, name)
        if parsed == 0 or parsed > maximum:
            raise RpcValidationError(
                f"{name} must be between 1 and {maximum}", code=INVALID_PARAMS
            )
        return parsed

    @staticmethod
    def _budget(value: object, fallback: AgentBudget | None = None) -> AgentBudget:
        defaults = (fallback or AgentBudget()).to_dict()
        if value is not None:
            if not isinstance(value, Mapping):
                raise RpcValidationError("budget must be a JSON object", code=INVALID_PARAMS)
            defaults.update(dict(value))
        return AgentBudget.from_dict(defaults)

    @staticmethod
    def _workspace(value: object, *, default_root: str) -> WorkspaceScope:
        if value is None:
            return WorkspaceScope(root=default_root)
        if not isinstance(value, Mapping):
            raise RpcValidationError("workspace must be a JSON object", code=INVALID_PARAMS)
        return WorkspaceScope.from_dict(dict(value))

    def emit_event(
        self,
        event_type: str,
        *,
        session_id: str,
        data: Mapping[str, Any],
        run_id: str | None = None,
        seq: int | None = None,
        ephemeral: bool = False,
    ) -> JsonObject:
        """Build an event notification; transport writing is intentionally separate."""

        if not event_type or not session_id:
            raise ValueError("event_type and session_id must be non-empty")
        if not isinstance(ephemeral, bool):
            raise TypeError("ephemeral must be boolean")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
            raise ValueError("seq must be a positive integer")
        params: JsonObject = {
            "protocol_version": PROTOCOL_VERSION,
            "event": {
                "session_id": session_id,
                "event_type": event_type,
                "ephemeral": ephemeral,
                "data": dict(data),
            },
        }
        if run_id is not None:
            params["event"]["run_id"] = run_id
        if seq is not None:
            params["event"]["seq"] = seq
        # Fail before a notification reaches stdout if the payload is not durable JSON.
        json.dumps(params, ensure_ascii=False, allow_nan=False)
        return _notification("event", params)

    def _initialize(
        self,
        request_id: JsonRpcId | None,
        params: JsonObject,
        is_notification: bool,
    ) -> JsonObject | None:
        raw_version = params.get("protocol_version", PROTOCOL_VERSION)
        if raw_version != PROTOCOL_VERSION:
            return self._maybe_error(
                is_notification,
                request_id,
                INVALID_PARAMS,
                f"Unsupported protocol_version: {raw_version!r}",
            )
        raw_store_path = params.get("store_path")
        if raw_store_path is not None:
            if not isinstance(raw_store_path, str) or not raw_store_path.strip():
                raise RpcValidationError(
                    "store_path must be a non-empty string", code=INVALID_PARAMS
                )
            requested_path = str(Path(raw_store_path).expanduser().resolve())
            if self._store is not None and self._store_path is None:
                raise RpcValidationError(
                    "store_path cannot replace an injected store", code=INVALID_PARAMS
                )
            if self._store_path is not None:
                configured_path = str(Path(self._store_path).expanduser().resolve())
                if configured_path != requested_path:
                    raise RpcValidationError(
                        "initialize store_path does not match the Worker configuration",
                        code=INVALID_PARAMS,
                    )
            self._store_path = requested_path
        self._ensure_store()
        self.initialized = True
        if is_notification:
            return None
        if request_id is None:
            return _error_response(None, INVALID_REQUEST, "initialize requires a request id")
        return _result_response(
            request_id,
            {
                "protocol_version": PROTOCOL_VERSION,
                "server_name": SERVER_NAME,
                "capabilities": {
                    "events": True,
                    "commands": [deepcopy(command) for command in COMMAND_DESCRIPTORS],
                },
                "storage": self._storage_descriptor(),
            },
        )

    def _storage_descriptor(self) -> JsonObject:
        if isinstance(self._store, SQLiteEventStore):
            return {"kind": "sqlite", "path": self._store_path}
        return {"kind": "memory"}

    @staticmethod
    def _maybe_error(
        is_notification: bool,
        request_id: JsonRpcId | None,
        code: int,
        message: str,
    ) -> JsonObject | None:
        return None if is_notification else _error_response(request_id, code, message)


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
    "WorkerServer",
]
