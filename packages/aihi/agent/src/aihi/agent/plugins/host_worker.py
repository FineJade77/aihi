"""Child-process implementation for the versioned Plugin Host protocol.

This module is intentionally launched in a separate process.  The parent only
passes a manifest entrypoint and JSON values across the boundary; it never
imports the plugin module itself.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO

from aihi.agent.mcp.protocol import McpCallResult, McpToolDefinition
from aihi.agent.plugins.host_protocol import (
    PLUGIN_HOST_PROTOCOL_VERSION,
    make_error,
    make_result,
    validate_request,
)

_MAX_MESSAGE_BYTES = 1_048_576
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_PLUGIN_FAILURE = -32000


def _load_entrypoint(root: Path, entrypoint: str) -> Any:
    module_name, separator, attribute = entrypoint.partition(":")
    sys.path.insert(0, str(root))
    module = importlib.import_module(module_name)
    value: Any = module
    if separator:
        value = getattr(module, attribute)
    if callable(value):
        value = value()
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _plugin_tools(plugin: Any) -> list[dict[str, Any]]:
    raw = getattr(plugin, "tools", ())
    if callable(raw):
        raw = raw()
    if isinstance(raw, dict):
        values: list[Any] = []
        for name, definition in raw.items():
            if not isinstance(name, str):
                raise ValueError("Plugin tool names must be strings")
            if isinstance(definition, McpToolDefinition):
                values.append(definition.to_dict())
                continue
            if not isinstance(definition, dict):
                raise ValueError("Plugin tools must be JSON objects")
            value = dict(definition)
            value.setdefault("name", name)
            value.pop("handler", None)
            values.append(value)
        raw = values
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Plugin tools must be an array or object")
    definitions = [McpToolDefinition.from_dict(item).to_dict() for item in raw]
    names = [str(item["name"]) for item in definitions]
    if len(names) != len(set(names)):
        raise ValueError("Plugin tools cannot contain duplicate names")
    return definitions


def _available_capabilities(plugin: Any) -> list[str]:
    capabilities: set[str] = set()
    if hasattr(plugin, "tools") or callable(getattr(plugin, "call_tool", None)):
        capabilities.add("tool")
    if hasattr(plugin, "skills") or callable(getattr(plugin, "load_skill", None)):
        capabilities.add("skill")
    if hasattr(plugin, "hooks") or callable(getattr(plugin, "emit_hook", None)):
        capabilities.add("hook")
    return sorted(capabilities)


async def _call_tool(plugin: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    method = getattr(plugin, "call_tool", None)
    if callable(method):
        value = await _maybe_await(method(name, dict(arguments)))
    else:
        tools = getattr(plugin, "tools", {})
        if not isinstance(tools, dict) or name not in tools:
            raise KeyError(name)
        handler = tools[name]
        if isinstance(handler, dict):
            handler = handler.get("handler")
        if not callable(handler):
            raise ValueError("Plugin tool has no callable handler")
        value = await _maybe_await(handler(dict(arguments)))
    if isinstance(value, McpCallResult):
        return value.to_dict()
    if isinstance(value, str):
        return {"content": [{"type": "text", "text": value}], "isError": False}
    return McpCallResult.from_dict(value).to_dict()


async def _load_skill(plugin: Any, name: str) -> dict[str, Any]:
    method = getattr(plugin, "load_skill", None)
    if callable(method):
        value = await _maybe_await(method(name))
    else:
        skills = getattr(plugin, "skills", {})
        if not isinstance(skills, dict) or name not in skills:
            raise KeyError(name)
        value = skills[name]
    if isinstance(value, str):
        return {"name": name, "body": value}
    if not isinstance(value, dict) or not isinstance(value.get("body"), str):
        raise ValueError("Plugin skill must return a body string")
    return {"name": str(value.get("name", name)), "body": value["body"]}


async def _emit_hook(plugin: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    method = getattr(plugin, "emit_hook", None)
    if callable(method):
        value = await _maybe_await(method(name, dict(payload)))
    else:
        hooks = getattr(plugin, "hooks", {})
        if not isinstance(hooks, dict) or name not in hooks:
            raise KeyError(name)
        handler = hooks[name]
        if not callable(handler):
            raise ValueError("Plugin hook has no callable handler")
        value = await _maybe_await(handler(dict(payload)))
    if value is None:
        return {"ok": True}
    if not isinstance(value, dict):
        raise ValueError("Plugin hook result must be an object")
    return dict(value)


async def _dispatch(
    plugin: Any, method: str, params: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if method == "initialize":
        if params.get("protocol_version") != PLUGIN_HOST_PROTOCOL_VERSION:
            raise ValueError("Plugin Host protocol version mismatch")
        return {
            "protocol_version": PLUGIN_HOST_PROTOCOL_VERSION,
            "capabilities": _available_capabilities(plugin),
        }, False
    if method == "tools/list":
        return {"tools": _plugin_tools(plugin)}, False
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise TypeError("tools/call requires name and object arguments")
        return await _call_tool(plugin, name, arguments), False
    if method == "skills/load":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise TypeError("skills/load requires a skill name")
        return await _load_skill(plugin, name), False
    if method == "hooks/emit":
        name = params.get("name")
        payload = params.get("payload", {})
        if not isinstance(name, str) or not name or not isinstance(payload, dict):
            raise TypeError("hooks/emit requires a name and object payload")
        return await _emit_hook(plugin, name, payload), False
    if method == "shutdown":
        return {"ok": True}, True
    raise LookupError(method)


async def _serve(plugin: Any, protocol_stdout: BinaryIO) -> None:
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline, _MAX_MESSAGE_BYTES + 1)
        if not raw:
            return
        if len(raw) > _MAX_MESSAGE_BYTES:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            request_id, method, params = validate_request(request)
        except Exception:  # noqa: BLE001 - malformed input ends the isolated worker.
            return
        try:
            result, should_stop = await _dispatch(plugin, method, params)
            response = make_result(request_id, result)
        except LookupError:
            response = make_error(request_id, _METHOD_NOT_FOUND, "Plugin method is not available")
            should_stop = False
        except TypeError:
            response = make_error(
                request_id, _INVALID_PARAMS, "Plugin method parameters are invalid"
            )
            should_stop = False
        except Exception:  # noqa: BLE001 - never return plugin traces or secrets.
            response = make_error(request_id, _PLUGIN_FAILURE, "Plugin operation failed")
            should_stop = False
        try:
            encoded = (
                (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                .encode("utf-8")
            )
            if len(encoded) > _MAX_MESSAGE_BYTES:
                return
            protocol_stdout.write(encoded)
            protocol_stdout.flush()
        except (BrokenPipeError, OSError):
            return
        if should_stop:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="AIHI isolated Plugin Host worker")
    parser.add_argument("--root", required=True)
    parser.add_argument("--entrypoint", required=True)
    args = parser.parse_args()
    try:
        # Plugin code must never be able to corrupt the JSON-lines protocol by
        # printing to stdout.  Its incidental output is intentionally discarded
        # by the parent through stderr.
        protocol_stdout = sys.stdout.buffer
        sys.stdout = sys.stderr
        plugin = _load_entrypoint(Path(args.root).resolve(), args.entrypoint)
        asyncio.run(_serve(plugin, protocol_stdout))
    except Exception:  # noqa: BLE001 - child failures are observed as a crashed host.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
