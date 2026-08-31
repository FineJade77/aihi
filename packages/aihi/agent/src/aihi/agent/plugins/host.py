"""Isolated, explicitly governed Plugin Host activation and adapters."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aihi.models as models_api
from aihi.agent.mcp.protocol import McpCallResult, McpToolDefinition
from aihi.agent.plugins.discovery import PluginCandidate, PluginDiscovery
from aihi.agent.plugins.errors import (
    PluginCapabilityDenied,
    PluginHostCrashed,
    PluginHostError,
    PluginHostOperationError,
    PluginHostProtocolError,
    PluginHostTimeout,
)
from aihi.agent.plugins.host_protocol import (
    PLUGIN_HOST_PROTOCOL_VERSION,
    make_request,
    validate_response,
)
from aihi.agent.plugins.trust import PluginTrustManager
from aihi.agent.tools.base import ToolContext, ToolExecutionResult, validate_tool_input
from aihi.agent.tools.spec import ToolSpec


@dataclass(frozen=True, slots=True)
class PluginHostPolicy:
    """Explicit activation allowlist; plugin declarations may only be a subset."""

    allowed_capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_permissions: frozenset[str] = field(default_factory=frozenset)
    run_id: str | None = None

    def __post_init__(self) -> None:
        capabilities = frozenset(self.allowed_capabilities)
        permissions = frozenset(self.allowed_permissions)
        if any(not isinstance(item, str) or not item for item in capabilities):
            raise ValueError("Plugin Host capabilities must be non-empty strings")
        if any(not isinstance(item, str) or not item for item in permissions):
            raise ValueError("Plugin Host permissions must be non-empty strings")
        if self.run_id is not None and not self.run_id.strip():
            raise ValueError("Plugin Host run_id must be non-empty when supplied")
        object.__setattr__(self, "allowed_capabilities", capabilities)
        object.__setattr__(self, "allowed_permissions", permissions)

    def validate(self, candidate: PluginCandidate) -> None:
        manifest = candidate.manifest
        missing_capabilities = sorted(
            set(manifest.capabilities) - self.allowed_capabilities
        )
        missing_permissions = sorted(set(manifest.permissions) - self.allowed_permissions)
        if missing_capabilities or missing_permissions:
            raise PluginCapabilityDenied(
                f"Plugin activation exceeds the Host policy: {candidate.key}",
                details={
                    "plugin": candidate.key,
                    "missing_capabilities": missing_capabilities,
                    "missing_permissions": missing_permissions,
                },
            )


class PluginHost:
    """Own one trusted plugin subprocess and expose only JSON value contracts."""

    def __init__(
        self,
        candidate: PluginCandidate,
        trust_manager: PluginTrustManager,
        *,
        discovery: PluginDiscovery | None = None,
        policy: PluginHostPolicy | None = None,
        request_timeout_seconds: float = 10.0,
        max_message_bytes: int = 1_048_576,
        stop_timeout_seconds: float = 2.0,
        python_executable: str | Path = sys.executable,
        worker_command: tuple[str, ...] | None = None,
    ) -> None:
        if (
            not isinstance(request_timeout_seconds, int | float)
            or isinstance(request_timeout_seconds, bool)
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("Plugin Host request timeout must be finite and positive")
        if (
            not isinstance(max_message_bytes, int)
            or isinstance(max_message_bytes, bool)
            or max_message_bytes <= 0
        ):
            raise ValueError("Plugin Host max_message_bytes must be positive")
        if (
            not isinstance(stop_timeout_seconds, int | float)
            or isinstance(stop_timeout_seconds, bool)
            or not math.isfinite(stop_timeout_seconds)
            or stop_timeout_seconds <= 0
        ):
            raise ValueError("Plugin Host stop timeout must be finite and positive")
        if worker_command is not None and (
            not worker_command
            or any(not isinstance(item, str) or not item for item in worker_command)
        ):
            raise ValueError("Plugin Host worker_command must contain non-empty strings")
        self.candidate = candidate
        self.trust_manager = trust_manager
        self.discovery = discovery
        self.policy = policy or PluginHostPolicy()
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_message_bytes = max_message_bytes
        self.stop_timeout_seconds = float(stop_timeout_seconds)
        self.python_executable = str(python_executable)
        self.worker_command = worker_command
        self._process: subprocess.Popen[bytes] | None = None
        self._active_candidate: PluginCandidate | None = None
        self._capabilities: frozenset[str] = frozenset()
        self._tools: dict[str, McpToolDefinition] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def active_candidate(self) -> PluginCandidate | None:
        return self._active_candidate

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            verifier = self.discovery or self.trust_manager.discovery
            activated = self.trust_manager.require_activatable(
                self.candidate,
                discovery=verifier,
            )
            self.policy.validate(activated)
            if not activated.manifest.entrypoint:
                raise PluginHostProtocolError(
                    f"Plugin has no executable entrypoint: {activated.key}"
                )
            command = list(
                self.worker_command
                or (self.python_executable, "-m", "aihi.agent.plugins.host_worker")
            )
            command.extend(
                (
                    "--root",
                    str(activated.root),
                    "--entrypoint",
                    activated.manifest.entrypoint,
                )
            )
            # ``-m aihi.agent...`` needs the directory that contains the
            # namespace package, not ``.../src/aihi`` itself.  Make inherited
            # relative entries absolute before changing the worker's cwd to
            # the plugin root.
            source_root = Path(__file__).resolve().parents[3]
            models_source_root = Path(models_api.__file__).resolve().parents[2]
            pythonpath = [str(source_root), str(models_source_root), str(activated.root)]
            inherited_pythonpath = os.environ.get("PYTHONPATH")
            if inherited_pythonpath:
                pythonpath.extend(
                    str(Path(entry).resolve())
                    for entry in inherited_pythonpath.split(os.pathsep)
                    if entry
                )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join(pythonpath),
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(activated.root),
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise PluginHostCrashed("Could not start Plugin Host") from exc
            self._active_candidate = activated
            try:
                result = await self._request_locked(
                    "initialize",
                    {
                        "protocol_version": PLUGIN_HOST_PROTOCOL_VERSION,
                        "plugin_id": activated.manifest.plugin_id,
                        "manifest_capabilities": list(activated.manifest.capabilities),
                    },
                )
                if result.get("protocol_version") != PLUGIN_HOST_PROTOCOL_VERSION:
                    raise PluginHostProtocolError("Plugin Host protocol version mismatch")
                raw_capabilities = result.get("capabilities", [])
                if not isinstance(raw_capabilities, list) or any(
                    not isinstance(item, str) for item in raw_capabilities
                ):
                    raise PluginHostProtocolError("Plugin Host capabilities are invalid")
                capabilities = frozenset(raw_capabilities)
                if not capabilities.issubset(set(activated.manifest.capabilities)):
                    raise PluginHostProtocolError(
                        "Plugin Host returned capabilities outside its manifest"
                    )
                self._capabilities = capabilities
            except BaseException:
                await self._terminate_locked()
                self._active_candidate = None
                self._capabilities = frozenset()
                self._tools = {}
                raise

    async def stop(self) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                self._active_candidate = None
                self._capabilities = frozenset()
                self._tools = {}
                return
            try:
                if process.poll() is None:
                    try:
                        await self._request_locked("shutdown", {})
                    except PluginHostError:
                        pass
            finally:
                await self._terminate_locked()
                self._active_candidate = None
                self._capabilities = frozenset()
                self._tools = {}

    async def __aenter__(self) -> PluginHost:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.stop()

    async def list_tools(self) -> tuple[McpToolDefinition, ...]:
        self._require_capability("tool")
        result = await self._request("tools/list", {})
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            async with self._lock:
                await self._terminate_locked()
            raise PluginHostProtocolError("Plugin Host tools/list lacks a tools array")
        try:
            definitions = tuple(McpToolDefinition.from_dict(item) for item in raw_tools)
            names = [definition.name for definition in definitions]
            if len(names) != len(set(names)):
                raise PluginHostProtocolError("Plugin Host returned duplicate tool names")
        except Exception as exc:  # noqa: BLE001 - protocol failures stop the Host.
            async with self._lock:
                await self._terminate_locked()
            if isinstance(exc, PluginHostProtocolError):
                raise
            raise PluginHostProtocolError("Plugin Host returned invalid tool definitions") from exc
        self._tools = {definition.name: definition for definition in definitions}
        return definitions

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        self._require_capability("tool")
        if not isinstance(name, str) or not name:
            raise PluginHostProtocolError("Plugin Host tool name must be non-empty")
        if not isinstance(arguments, dict):
            raise PluginHostProtocolError("Plugin Host tool arguments must be an object")
        definition = self._tools.get(name)
        if definition is None:
            await self.list_tools()
            definition = self._tools.get(name)
        if definition is None:
            raise PluginHostProtocolError(f"Plugin Host tool was not discovered: {name}")
        try:
            validate_tool_input(definition.to_tool_spec(), arguments)
        except Exception as exc:  # noqa: BLE001 - normalize schema errors at the boundary.
            raise PluginHostProtocolError(
                f"Invalid arguments for Plugin Host tool: {name}"
            ) from exc
        result = await self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        try:
            return McpCallResult.from_dict(result)
        except Exception as exc:  # noqa: BLE001 - protocol failures stop the Host.
            async with self._lock:
                await self._terminate_locked()
            raise PluginHostProtocolError("Plugin Host returned an invalid tool result") from exc

    async def load_skill(self, name: str) -> str:
        self._require_capability("skill")
        if not isinstance(name, str) or not name:
            raise PluginHostProtocolError("Plugin Host skill name must be non-empty")
        result = await self._request("skills/load", {"name": name})
        body = result.get("body")
        if not isinstance(body, str):
            async with self._lock:
                await self._terminate_locked()
            raise PluginHostProtocolError("Plugin Host skill body must be a string")
        return body

    async def emit_hook(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_capability("hook")
        if not isinstance(name, str) or not name:
            raise PluginHostProtocolError("Plugin Host hook name must be non-empty")
        if not isinstance(payload, dict):
            raise PluginHostProtocolError("Plugin Host hook payload must be an object")
        return await self._request("hooks/emit", {"name": name, "payload": dict(payload)})

    async def remote_tools(self) -> tuple[PluginRemoteTool, ...]:
        definitions = await self.list_tools()
        return tuple(
            PluginRemoteTool(host=self, definition=definition)
            for definition in definitions
        )

    def _require_capability(self, capability: str) -> None:
        if not self.running or self._active_candidate is None:
            raise PluginHostCrashed("Plugin Host is not running")
        if capability not in self._capabilities:
            raise PluginCapabilityDenied(
                f"Plugin Host capability is unavailable: {capability}",
                details={"plugin": self._active_candidate.key, "capability": capability},
            )

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await self._request_locked(method, params)

    async def _request_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            raise PluginHostCrashed(
                "Plugin Host process is not running",
                details={"returncode": process.poll() if process is not None else None},
            )
        if process.stdin is None or process.stdout is None:
            raise PluginHostCrashed("Plugin Host process pipes are unavailable")
        self._next_id += 1
        request_id = self._next_id
        read_task: asyncio.Task[bytes] | None = None
        try:
            request = make_request(request_id, method, params)
            encoded = (
                json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            if len(encoded) > self.max_message_bytes:
                raise PluginHostProtocolError("Plugin Host request exceeds the size limit")
            await asyncio.to_thread(process.stdin.write, encoded)
            await asyncio.to_thread(process.stdin.flush)
            read_task = asyncio.create_task(
                asyncio.to_thread(process.stdout.readline, self.max_message_bytes + 1)
            )
            raw = await asyncio.wait_for(
                asyncio.shield(read_task),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            if read_task is not None:
                read_task.cancel()
            cleanup = asyncio.create_task(self._terminate_locked())
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup),
                    timeout=max(1.0, self.stop_timeout_seconds * 2),
                )
            except (TimeoutError, asyncio.CancelledError):
                cleanup.cancel()
            raise
        except TimeoutError as exc:
            if read_task is not None:
                read_task.cancel()
            await self._terminate_locked()
            raise PluginHostTimeout(
                f"Plugin Host request timed out: {method}",
                details={"method": method},
            ) from exc
        except (BrokenPipeError, OSError) as exc:
            await self._terminate_locked()
            raise PluginHostCrashed(f"Plugin Host request failed: {method}") from exc
        if not raw:
            returncode = process.poll()
            await self._terminate_locked()
            raise PluginHostCrashed(
                f"Plugin Host exited during request: {method}",
                details={"returncode": returncode},
            )
        if len(raw) > self.max_message_bytes:
            await self._terminate_locked()
            raise PluginHostProtocolError("Plugin Host response exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
            response = validate_response(value, request_id)
        except (UnicodeDecodeError, json.JSONDecodeError, PluginHostProtocolError) as exc:
            await self._terminate_locked()
            if isinstance(exc, PluginHostProtocolError):
                raise
            raise PluginHostProtocolError("Plugin Host response is not valid JSON") from exc
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise PluginHostOperationError(
                "Plugin Host operation returned an error",
                details={"method": method, "remote_code": code},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            await self._terminate_locked()
            raise PluginHostProtocolError("Plugin Host result is not an object")
        return result

    async def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        # End the process before touching its pipes: closing a BufferedReader
        # whose readline runs in the executor deadlocks on the buffer lock, and
        # SIGKILL always delivers the EOF that wakes it. Close through the file
        # objects rather than os.close(), because Popen finalization closes the
        # same descriptors again -- by then the number may belong to another
        # file, and closing it would corrupt an unrelated one.
        if process.poll() is None:
            self._signal_process_group(process, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, timeout=self.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process, signal.SIGKILL)
                try:
                    await asyncio.to_thread(process.wait, timeout=self.stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError, RuntimeError):
                pass

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), sig)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            if sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass


@dataclass(slots=True)
class PluginRemoteTool:
    host: PluginHost
    definition: McpToolDefinition

    @property
    def spec(self) -> ToolSpec:
        candidate = self.host.active_candidate
        plugin_id = candidate.manifest.plugin_id if candidate is not None else "plugin"
        return self.definition.to_tool_spec(
            exposed_name=f"plugin.{plugin_id}.{self.definition.name}"
        )

    async def run(self, input: dict[str, Any], context: ToolContext[Any]) -> ToolExecutionResult:
        result = await self.host.call_tool(self.definition.name, input)
        content_parts: list[str] = []
        for item in result.content:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                content_parts.append(item["text"])
            else:
                content_parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        if result.structured_content is not None:
            content_parts.append(
                json.dumps(result.structured_content, ensure_ascii=False, sort_keys=True)
            )
        return ToolExecutionResult(
            content="\n".join(content_parts),
            is_error=result.is_error,
            metadata={
                "plugin_id": self.host.active_candidate.manifest.plugin_id
                if self.host.active_candidate is not None
                else None,
                "plugin_tool": self.definition.name,
                "plugin_structured_content": result.structured_content,
            },
        )


__all__ = ["PluginHost", "PluginHostPolicy", "PluginRemoteTool"]
