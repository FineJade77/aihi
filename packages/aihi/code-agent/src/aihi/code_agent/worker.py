"""Worker process implementation and stdio transport entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, BinaryIO

from aihi.agent import (
    AgentBudget,
    AgentRuntimeError,
    AgentState,
    Approval,
    EventStore,
    FileSkillTrustStore,
    InMemoryEventStore,
    Session,
    SkillDiscovery,
    SkillRoot,
    SkillTrustManager,
    SQLiteEventStore,
    TaskGraph,
    WorkspaceScope,
)
from aihi.agent import (
    Event as AgentEvent,
)
from aihi.agent.runtime import RunResult
from aihi.code_agent.config import CodeAgentConfig, ensure_user_config, load_config
from aihi.code_agent.runtime import CodeAgentRuntime

from .framing import FrameError, read_frame, write_frame
from .protocol import (
    _COMMAND_NAMES,
    COMMAND_DESCRIPTORS,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SHUTTING_DOWN,
    JsonObject,
    JsonRpcId,
    RpcValidationError,
    _error_response,
    _notification,
    _result_response,
    _valid_id,
)
from .protocol import (
    _request_id as protocol_request_id,
)


class WorkerServer:
    """Dispatch lifecycle, Session, and Task commands for one Worker process."""

    def __init__(
        self,
        *,
        store: EventStore | None = None,
        store_path: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        if store is not None and store_path is not None:
            raise ValueError("store and store_path are mutually exclusive")
        self.initialized = False
        self.client_initialized = False
        self.shutdown_requested = False
        self._store: EventStore | None = store
        self._store_path = str(store_path) if store_path is not None else None
        self._config_path = (
            str(Path(config_path).expanduser().resolve()) if config_path is not None else None
        )
        self._owns_store = False
        self._sessions: dict[str, Session] = {}
        self._task_graphs: dict[str, TaskGraph] = {}
        self._pending_notifications: list[JsonObject] = []

    def handle(self, message: object) -> JsonObject | None:
        """Handle one decoded JSON value and return a response if required."""

        if not isinstance(message, Mapping):
            return _error_response(None, INVALID_REQUEST, "Request must be a JSON object")
        raw_id = message.get("id")
        request_id = protocol_request_id(raw_id) if "id" in message else None
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

    def handle_background(
        self, message: object, *, cancel_signal: threading.Event
    ) -> JsonObject | None:
        """Handle a long-running Run in a Worker thread while preserving RPC semantics."""

        if not isinstance(message, Mapping):
            return _error_response(None, INVALID_REQUEST, "Request must be a JSON object")
        raw_id = message.get("id")
        request_id = protocol_request_id(raw_id) if "id" in message else None
        if "id" not in message:
            return None
        if request_id is None:
            return _error_response(None, INVALID_REQUEST, "Request id must be a string or integer")
        if message.get("jsonrpc") != "2.0":
            return _error_response(request_id, INVALID_REQUEST, "jsonrpc must be '2.0'")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not method:
            return _error_response(request_id, INVALID_REQUEST, "method must be a non-empty string")
        if not isinstance(params, Mapping):
            return _error_response(request_id, INVALID_PARAMS, "params must be a JSON object")
        if method not in {"run.start", "run.resume"}:
            return self.handle(message)
        if not self.initialized:
            return _error_response(request_id, NOT_INITIALIZED, "initialize is required first")
        try:
            result = (
                self._run_start(dict(params), cancel_signal=cancel_signal)
                if method == "run.start"
                else self._run_resume(dict(params), cancel_signal=cancel_signal)
            )
        except RpcValidationError as error:
            return _error_response(request_id, error.code, str(error))
        except (AgentRuntimeError, TypeError, ValueError) as error:
            return _error_response(request_id, INVALID_PARAMS, str(error))
        return _result_response(request_id, result)

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
        if method == "session.usage":
            return self._session_usage(params)
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
        if method == "run.start":
            return self._run_start(params)
        if method == "run.resume":
            return self._run_resume(params)
        if method == "run.list":
            return self._run_list(params)
        if method == "run.cancel":
            return self._run_cancel(params)
        if method == "session.fork":
            return self._session_fork(params)
        if method == "config.get":
            return self._config_get(params)
        if method == "config.init":
            return self._config_init()
        if method == "approval.list":
            return self._approval_list(params)
        if method == "approval.resolve":
            return self._approval_resolve(params)
        if method == "skill.list":
            return self._skill_list(params)
        if method == "skill.trust":
            return self._skill_trust(params)
        if method == "skill.untrust":
            return self._skill_untrust(params)
        if method == "mcp.list":
            return self._mcp_list(params)
        if method == "tool.list":
            return self._tool_list(params)
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
        config = load_config(self._config_path, cwd=cwd)
        provider = self._optional_text_value(params.get("provider"), "provider", max_length=256)
        model = self._optional_text_value(params.get("model"), "model", max_length=256)
        resolved_provider = provider or config.provider.name
        resolved_model = model or config.provider.model
        session_id = params.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise RpcValidationError("session_id must be a non-empty string", code=INVALID_PARAMS)
        metadata = self._optional_mapping(params, "metadata")
        session = Session.create(
            self._ensure_store(),
            cwd=cwd,
            provider=resolved_provider,
            model=resolved_model,
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

    def _session_fork(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        at_seq = self._non_negative_int(params.get("at_seq", session.head_seq), "at_seq")
        if at_seq < 1:
            raise RpcValidationError("at_seq must be positive", code=INVALID_PARAMS)
        child = session.fork(at_seq=at_seq, event_observer=self._observe_event)
        self._sessions[child.id] = child
        return {"session": self._session_descriptor(child), "at_seq": at_seq}

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

    def _session_usage(self, params: JsonObject) -> JsonObject:
        """Total what this session has spent, and how full its context last was.

        Replayed from the log rather than accumulated in memory, so the answer
        survives a Worker restart and matches what actually happened.
        """

        session = self._load_session(params)
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
        cost_usd: float | None = None
        calls = 0
        context_tokens = 0
        context_limit = 0
        model = ""
        for event in self._ensure_store().read(session.id, after_seq=0):
            if event.type != "model.usage":
                continue
            calls += 1
            data = event.data
            for key in totals:
                value = data.get(key, 0)
                totals[key] += int(value) if isinstance(value, int) else 0
            raw_cost = data.get("cost_usd")
            if isinstance(raw_cost, int | float):
                cost_usd = (cost_usd or 0.0) + float(raw_cost)
            # The most recent call is the one describing the live context.
            context_tokens = int(data.get("context_tokens", context_tokens) or 0)
            context_limit = int(data.get("context_limit", context_limit) or 0)
            model = str(data.get("model", model))
        return {
            "session_id": session.id,
            "model_calls": calls,
            "model": model,
            "cost_usd": cost_usd,
            "context_tokens": context_tokens,
            "context_limit": context_limit,
            "context_used_ratio": (
                round(context_tokens / context_limit, 4) if context_limit else 0.0
            ),
            **totals,
            "total_tokens": totals["input_tokens"] + totals["output_tokens"],
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

    def _run_start(
        self, params: JsonObject, *, cancel_signal: threading.Event | None = None
    ) -> JsonObject:
        session = self._load_session(params)
        user_message = self._required_text(params, "user_message", max_length=100_000)
        provider = self._optional_text_value(params.get("provider"), "provider", max_length=256)
        model = self._optional_text_value(params.get("model"), "model", max_length=256)
        if "system_prompt" in params:
            raise RpcValidationError(
                "system_prompt is not accepted; the Coding Agent owns its prompt",
                code=INVALID_PARAMS,
            )
        max_output_tokens = self._optional_positive_int(
            params.get("max_output_tokens"), "max_output_tokens"
        )
        run_id = self._optional_text_value(params.get("run_id"), "run_id", max_length=256)
        config = load_config(self._config_path, cwd=session.cwd)
        config = config.select_provider(provider, model=model)
        return asyncio.run(
            self._execute_run_start(
                config,
                session,
                user_message=user_message,
                run_id=run_id,
                model=model,
                max_output_tokens=max_output_tokens,
                cancel_signal=cancel_signal,
            )
        )

    def _config_init(self) -> JsonObject:
        try:
            path, created = ensure_user_config()
        except OSError as error:
            raise RpcValidationError(
                f"Cannot create user configuration: {error}", code=INTERNAL_ERROR
            ) from error
        return {"path": str(path), "created": created}

    def _config_get(self, params: JsonObject) -> JsonObject:
        cwd = self._optional_text_value(params.get("cwd"), "cwd", max_length=4_096)
        config = load_config(self._config_path, cwd=cwd or str(Path.cwd()))
        return {"config": config.public_descriptor()}

    def _run_resume(
        self, params: JsonObject, *, cancel_signal: threading.Event | None = None
    ) -> JsonObject:
        session = self._load_session(params)
        run_id = self._required_text(params, "run_id", max_length=256)
        model = self._optional_text_value(params.get("model"), "model", max_length=256)
        if "system_prompt" in params:
            raise RpcValidationError(
                "system_prompt is not accepted; the Coding Agent owns its prompt",
                code=INVALID_PARAMS,
            )
        max_output_tokens = self._optional_positive_int(
            params.get("max_output_tokens"), "max_output_tokens"
        )
        config = load_config(self._config_path, cwd=session.cwd)
        return asyncio.run(
            self._execute_run_resume(
                config,
                session,
                run_id=run_id,
                model=model,
                max_output_tokens=max_output_tokens,
                cancel_signal=cancel_signal,
            )
        )

    def _run_list(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        selected: dict[str, JsonObject] = {}
        for event in session.events:
            if event.run_id is None:
                continue
            descriptor = selected.setdefault(
                event.run_id,
                {
                    "run_id": event.run_id,
                    "state": "created",
                    "started_at": event.created_at,
                    "updated_at": event.created_at,
                    "provider": None,
                    "model": None,
                    "error": None,
                    "pending_approval_id": None,
                },
            )
            descriptor["updated_at"] = event.created_at
            if event.type == "run.started":
                descriptor["state"] = "running"
                descriptor["provider"] = event.data.get("provider")
                descriptor["model"] = event.data.get("model")
            elif event.type == "run.resumed":
                descriptor["state"] = "running"
            elif event.type == "run.suspended":
                descriptor["state"] = "waiting_approval"
                descriptor["pending_approval_id"] = event.data.get("approval_id")
            elif event.type == "run.completed":
                descriptor["state"] = "completed"
            elif event.type == "run.failed":
                descriptor["state"] = "failed"
                descriptor["error"] = event.data.get("error")
            elif event.type == "run.interrupted":
                descriptor["state"] = "interrupted"
            elif event.type == "run.cancelled":
                descriptor["state"] = "cancelled"
        runs = sorted(selected.values(), key=lambda item: str(item["updated_at"]), reverse=True)
        return {"session_id": session.id, "runs": runs}

    def _run_cancel(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        run_id = self._required_text(params, "run_id", max_length=256)
        reason = self._optional_text_value(params.get("reason"), "reason", max_length=4_096)
        config = load_config(self._config_path, cwd=session.cwd)
        return asyncio.run(self._execute_run_cancel(config, session, run_id=run_id, reason=reason))

    @staticmethod
    async def _execute_run_cancel(
        config: CodeAgentConfig,
        session: Session,
        *,
        run_id: str,
        reason: str | None,
    ) -> JsonObject:
        runtime = await CodeAgentRuntime.create(config, store=session.store)
        try:
            result = runtime.runtime.coordinator.abandon(
                session, run_id=run_id, reason=reason or "cancelled by user"
            )
            return WorkerServer._run_result(result)
        finally:
            await runtime.close()

    @staticmethod
    async def _execute_run_start(
        config: CodeAgentConfig,
        session: Session,
        *,
        user_message: str,
        run_id: str | None,
        model: str | None,
        max_output_tokens: int | None,
        cancel_signal: threading.Event | None = None,
    ) -> JsonObject:
        runtime = await CodeAgentRuntime.create(config, store=session.store)
        cancel_event, watcher = await WorkerServer._cancel_bridge(cancel_signal)
        try:
            result = await runtime.run(
                session,
                user_message=user_message,
                run_id=run_id,
                model=model,
                max_output_tokens=max_output_tokens,
                cancel_event=cancel_event,
            )
            return WorkerServer._run_result(result)
        finally:
            if watcher is not None:
                watcher.cancel()
            await runtime.close()

    @staticmethod
    async def _execute_run_resume(
        config: CodeAgentConfig,
        session: Session,
        *,
        run_id: str,
        model: str | None,
        max_output_tokens: int | None,
        cancel_signal: threading.Event | None = None,
    ) -> JsonObject:
        runtime = await CodeAgentRuntime.create(config, store=session.store)
        cancel_event, watcher = await WorkerServer._cancel_bridge(cancel_signal)
        try:
            result = await runtime.resume(
                session,
                run_id=run_id,
                model=model,
                max_output_tokens=max_output_tokens,
                cancel_event=cancel_event,
            )
            return WorkerServer._run_result(result)
        finally:
            if watcher is not None:
                watcher.cancel()
            await runtime.close()

    @staticmethod
    async def _cancel_bridge(
        signal: threading.Event | None,
    ) -> tuple[asyncio.Event | None, asyncio.Task[None] | None]:
        if signal is None:
            return None, None
        event = asyncio.Event()

        async def watch() -> None:
            while not signal.is_set():
                await asyncio.sleep(0.05)
            event.set()

        return event, asyncio.create_task(watch())

    @staticmethod
    def _run_result(run_result: RunResult) -> JsonObject:
        response = run_result.response
        payload: JsonObject = {
            "run_id": run_result.run_id,
            "state": run_result.state.value,
            "suspended": run_result.suspended,
            "error": run_result.error,
            "pending_approval_id": run_result.pending_approval_id,
            "pending_tool_call_ids": list(run_result.pending_tool_call_ids),
        }
        if response is not None:
            payload["response"] = {
                "message": response.message.to_dict(),
                "stop_reason": response.stop_reason,
                "usage": response.usage.to_dict(),
            }
        return payload

    def _approval_list(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        run_id = self._optional_text_value(params.get("run_id"), "run_id", max_length=256)
        approvals = [
            approval
            for approval in session.authorization.pending_approvals.values()
            if approval.active() and (run_id is None or approval.run_id == run_id)
        ]
        approvals.sort(key=lambda item: item.approval_id)
        return {
            "session_id": session.id,
            "approvals": [self._approval_descriptor(session, approval) for approval in approvals],
        }

    def _approval_resolve(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        approval_id = self._required_text(params, "approval_id", max_length=256)
        approved = params.get("approved")
        if not isinstance(approved, bool):
            raise RpcValidationError("approved must be a boolean", code=INVALID_PARAMS)
        one_shot = params.get("one_shot", False)
        if not isinstance(one_shot, bool):
            raise RpcValidationError("one_shot must be a boolean", code=INVALID_PARAMS)
        resolved_by = self._optional_text_value(
            params.get("resolved_by"), "resolved_by", max_length=256
        ) or "cli"
        approval = session.authorization.pending_approval(approval_id)
        if approval is None or approval.run_id is None:
            raise RpcValidationError(
                f"No active pending approval: {approval_id}", code=INVALID_PARAMS
            )
        session.resolve_approval(
            approval_id,
            approved=approved,
            resolved_by=resolved_by,
            run_id=approval.run_id,
            one_shot=one_shot if approved else False,
        )
        return {
            "session_id": session.id,
            "approval_id": approval_id,
            "approved": approved,
            "one_shot": one_shot if approved else False,
        }

    def _skill_list(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        config = load_config(self._config_path, cwd=session.cwd)
        discovery, trust = self._skill_components(config)
        candidates = discovery.discover()
        return {
            "session_id": session.id,
            "skills": [
                {
                    "name": candidate.frontmatter.name,
                    "version": candidate.frontmatter.version,
                    "scope": candidate.scope.value,
                    "path": str(candidate.document_path),
                    "content_sha256": candidate.content_sha256,
                    "trusted": trust.status(candidate).trusted,
                    "enabled": trust.status(candidate).enabled,
                    "loadable": trust.status(candidate).loadable,
                }
                for candidate in candidates
            ],
        }

    def _skill_trust(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        name = self._required_text(params, "name", max_length=256)
        trusted_by = self._optional_text_value(
            params.get("trusted_by"), "trusted_by", max_length=256
        ) or "cli"
        enable = params.get("enable", True)
        if not isinstance(enable, bool):
            raise RpcValidationError("enable must be a boolean", code=INVALID_PARAMS)
        config = load_config(self._config_path, cwd=session.cwd)
        discovery, trust = self._skill_components(config)
        candidate = next((item for item in discovery.discover() if item.key == name), None)
        if candidate is None:
            raise RpcValidationError(f"Skill was not discovered: {name}", code=INVALID_PARAMS)
        record = trust.trust(candidate, trusted_by=trusted_by, enable=enable)
        return {"session_id": session.id, "skill": record.to_dict()}

    def _skill_untrust(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        name = self._required_text(params, "name", max_length=256)
        config = load_config(self._config_path, cwd=session.cwd)
        discovery, trust = self._skill_components(config)
        candidate = next((item for item in discovery.discover() if item.key == name), None)
        if candidate is None:
            raise RpcValidationError(f"Skill was not discovered: {name}", code=INVALID_PARAMS)
        trust.store.remove(
            candidate.frontmatter.name, candidate.frontmatter.version, candidate.scope
        )
        return {"session_id": session.id, "name": name, "removed": True}

    def _mcp_list(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        config = load_config(self._config_path, cwd=session.cwd)
        return {
            "session_id": session.id,
            "servers": [
                {
                    "name": server.name,
                    "command": list(server.command),
                    "cwd": str(server.cwd) if server.cwd else None,
                    "env_keys": sorted(server.env),
                    "allowed_tools": (
                        sorted(server.allowed_tools) if server.allowed_tools is not None else None
                    ),
                    "request_timeout_seconds": server.request_timeout_seconds,
                    "reconnect_attempts": server.reconnect_attempts,
                    "applies_to": "new_run",
                }
                for server in config.mcp_servers
            ],
        }

    def _tool_list(self, params: JsonObject) -> JsonObject:
        session = self._load_session(params)
        config = load_config(self._config_path, cwd=session.cwd)
        names = list(config.tools)
        if config.skill_load_tool and config.skill_roots and "load_skill" not in names:
            names.append("load_skill")
        return {
            "session_id": session.id,
            "tools": [{"name": name, "configured": True} for name in names],
        }

    @staticmethod
    def _skill_components(config: CodeAgentConfig) -> tuple[SkillDiscovery, SkillTrustManager]:
        if not config.skill_roots or config.skill_trust_path is None:
            raise RpcValidationError(
                "No Skill roots are configured; add [[skills.roots]] first",
                code=INVALID_PARAMS,
            )
        discovery = SkillDiscovery(
            [SkillRoot(root.path, root.scope) for root in config.skill_roots]
        )
        trust_store = FileSkillTrustStore(config.skill_trust_path)
        return discovery, SkillTrustManager(trust_store, discovery=discovery)

    @staticmethod
    def _approval_descriptor(session: Session, approval: Approval) -> JsonObject:
        approval_id = approval.approval_id
        descriptor = dict(approval.to_dict())
        for event in reversed(session.events):
            if event.type != "approval.requested":
                continue
            raw = event.data.get("approval")
            if not isinstance(raw, dict) or raw.get("approval_id") != approval_id:
                continue
            for key in ("requested_by", "tool_call_id", "tool_name", "rule_id", "reason"):
                if key in event.data:
                    descriptor[key] = event.data[key]
            break
        return descriptor

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

    def _task_event_sink(self, event: AgentEvent) -> None:
        session = self._sessions.get(event.session_id)
        if session is None:
            raise RpcValidationError(
                "Task event references an unloaded session", code=INTERNAL_ERROR
            )
        session.append(event)

    def _observe_event(self, event: AgentEvent) -> None:
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
        raw_config_path = params.get("config_path")
        if raw_config_path is not None:
            if not isinstance(raw_config_path, str) or not raw_config_path.strip():
                raise RpcValidationError(
                    "config_path must be a non-empty string", code=INVALID_PARAMS
                )
            requested_config = str(Path(raw_config_path).expanduser().resolve())
            if self._config_path is not None and self._config_path != requested_config:
                raise RpcValidationError(
                    "initialize config_path does not match the Worker configuration",
                    code=INVALID_PARAMS,
                )
            self._config_path = requested_config
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
    def _optional_text_value(
        value: object, key: str, *, max_length: int = 4_096
    ) -> str | None:
        if value is None:
            return None
        return WorkerServer._optional_text(value, key, max_length=max_length)

    @staticmethod
    def _optional_positive_int(value: object, key: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RpcValidationError(f"{key} must be a positive integer", code=INVALID_PARAMS)
        return value

    @staticmethod
    def _maybe_error(
        is_notification: bool,
        request_id: JsonRpcId | None,
        code: int,
        message: str,
    ) -> JsonObject | None:
        return None if is_notification else _error_response(request_id, code, message)

@dataclass(slots=True)
class _PendingRun:
    request_id: str | int
    run_id: str
    cancel_signal: Event
    future: Future[dict[str, Any] | None]


def _request_id(message: object) -> str | int | None:
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _method(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    value = message.get("method")
    return value if isinstance(value, str) else None


def _params(message: object) -> dict[str, Any]:
    if not isinstance(message, dict) or not isinstance(message.get("params", {}), dict):
        return {}
    return dict(message.get("params", {}))


def serve_stdio(
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    stderr: Any = None,
    server: WorkerServer | None = None,
) -> int:
    """Serve framed requests until EOF, shutdown, or an unrecoverable frame error."""

    runtime = server or WorkerServer(
        store_path=os.environ.get("AIHI_CODE_AGENT_STORE"),
        config_path=os.environ.get("AIHI_CODE_AGENT_CONFIG"),
    )
    error_stream = sys.stderr if stderr is None else stderr
    incoming: Queue[tuple[str, object]] = Queue()
    eof = Event()

    def read_loop() -> None:
        try:
            while True:
                raw = read_frame(stdin)
                if raw is None:
                    incoming.put(("eof", None))
                    return
                incoming.put(("frame", raw))
        except FrameError as error:
            incoming.put(("frame_error", error))

    reader = Thread(target=read_loop, name="aihi-code-agent-reader", daemon=True)
    reader.start()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="aihi-code-agent-run")
    pending: dict[str, _PendingRun] = {}

    def emit_notifications() -> None:
        for notification in runtime.drain_notifications():
            write_frame(stdout, notification)

    def finish_runs() -> None:
        for run_id, item in list(pending.items()):
            if not item.future.done():
                continue
            try:
                response = item.future.result()
            except Exception as error:  # noqa: BLE001 - protocol boundary must stay alive.
                print(f"aihi-code-agent worker internal error: {error}", file=error_stream)
                response = {
                    "jsonrpc": "2.0",
                    "id": item.request_id,
                    "error": {"code": INTERNAL_ERROR, "message": "Internal worker error"},
                }
            if response is not None:
                write_frame(stdout, response)
            pending.pop(run_id, None)
            emit_notifications()

    try:
        while True:
            finish_runs()
            emit_notifications()
            if eof.is_set() and not pending:
                return 0
            if runtime.shutdown_requested and not pending:
                return 0
            try:
                kind, payload = incoming.get(timeout=0.02)
            except Empty:
                continue
            if kind == "eof":
                eof.set()
                for item in pending.values():
                    item.cancel_signal.set()
                continue
            if kind == "frame_error":
                error = payload
                print(f"aihi-code-agent worker framing error: {error}", file=error_stream)
                return 2
            try:
                if not isinstance(payload, bytes):
                    raise TypeError("Worker frame payload must be bytes")
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                write_frame(stdout, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": PARSE_ERROR,
                    "message": "Invalid JSON payload",
                }})
                print(f"aihi-code-agent worker parse error: {error}", file=error_stream)
                continue

            method = _method(decoded)
            params = _params(decoded)
            request_id = _request_id(decoded)
            if method in {"run.start", "run.resume"} and request_id is not None:
                run_id = params.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    if method == "run.start":
                        run_id = f"run_worker_{request_id}"
                        if isinstance(decoded, dict):
                            decoded = dict(decoded)
                            decoded["params"] = {**params, "run_id": run_id}
                    else:
                        run_id = ""
                if run_id and run_id in pending:
                    write_frame(stdout, {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"Run is already active: {run_id}"},
                    })
                    continue
                signal = Event()
                future = executor.submit(
                    runtime.handle_background, decoded, cancel_signal=signal
                )
                if run_id:
                    pending[run_id] = _PendingRun(request_id, run_id, signal, future)
                else:
                    pending[f"request:{request_id}"] = _PendingRun(
                        request_id, f"request:{request_id}", signal, future
                    )
                continue
            if method == "run.cancel" and request_id is not None:
                run_id = params.get("run_id")
                if isinstance(run_id, str) and run_id in pending:
                    pending[run_id].cancel_signal.set()
                    write_frame(stdout, {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"run_id": run_id, "requested": True},
                    })
                    continue
            try:
                response = runtime.handle(decoded)
            except Exception as error:  # noqa: BLE001 - protocol boundary must stay alive.
                print(f"aihi-code-agent worker internal error: {error}", file=error_stream)
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": INTERNAL_ERROR, "message": "Internal worker error"},
                }
            if response is not None:
                write_frame(stdout, response)
            emit_notifications()
            if runtime.shutdown_requested:
                for item in pending.values():
                    item.cancel_signal.set()
    finally:
        for item in pending.values():
            item.cancel_signal.set()
        executor.shutdown(wait=True, cancel_futures=False)
        reader.join(timeout=0.2)
        runtime.close()


def main() -> int:
    return serve_stdio(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - exercised by the installed entry point.
    raise SystemExit(main())


__all__ = ["WorkerServer", "main", "serve_stdio"]
