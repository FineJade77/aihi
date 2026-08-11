"""Versioned JSON-lines protocol used by the isolated Plugin Host."""

from __future__ import annotations

from typing import Any

from aihi.agent.plugins.errors import PluginHostProtocolError
from aihi.models import JsonObject

PLUGIN_HOST_PROTOCOL_VERSION = "aiharness.plugin.v1"


def _validate_id(request_id: int) -> None:
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
        raise PluginHostProtocolError("Plugin Host request id must be a positive integer")


def make_request(request_id: int, method: str, params: JsonObject | None = None) -> JsonObject:
    _validate_id(request_id)
    if (
        not isinstance(method, str)
        or not method.strip()
        or (params is not None and not isinstance(params, dict))
    ):
        raise PluginHostProtocolError(
            "Plugin Host requests require a positive integer id and method"
        )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params if params is not None else {},
    }


def make_result(request_id: int, result: JsonObject) -> JsonObject:
    _validate_id(request_id)
    if not isinstance(result, dict):
        raise PluginHostProtocolError("Plugin Host results must be JSON objects")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: int, code: int, message: str) -> JsonObject:
    _validate_id(request_id)
    if (
        not isinstance(code, int)
        or isinstance(code, bool)
        or not isinstance(message, str)
        or not message
    ):
        raise PluginHostProtocolError("Plugin Host errors require an integer code and message")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def validate_request(value: object) -> tuple[int, str, JsonObject]:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise PluginHostProtocolError("Plugin Host request is not JSON-RPC 2.0")
    request_id = value.get("id")
    method = value.get("method")
    params = value.get("params", {})
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
        or not isinstance(method, str)
        or not method.strip()
        or not isinstance(params, dict)
    ):
        raise PluginHostProtocolError("Plugin Host request has invalid id, method, or params")
    if "result" in value or "error" in value:
        raise PluginHostProtocolError("Plugin Host request cannot contain result or error")
    return request_id, method, params


def validate_response(value: object, request_id: int) -> dict[str, Any]:
    _validate_id(request_id)
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise PluginHostProtocolError("Plugin Host response is not JSON-RPC 2.0")
    response_id = value.get("id")
    if (
        isinstance(response_id, bool)
        or not isinstance(response_id, int)
        or response_id != request_id
    ):
        raise PluginHostProtocolError("Plugin Host response id does not match request")
    has_result = "result" in value
    has_error = "error" in value
    if has_result == has_error:
        raise PluginHostProtocolError(
            "Plugin Host response must contain exactly one result or error"
        )
    if has_result:
        result = value["result"]
        if not isinstance(result, dict):
            raise PluginHostProtocolError("Plugin Host result must be an object")
    else:
        error = value["error"]
        if not isinstance(error, dict):
            raise PluginHostProtocolError("Plugin Host error must be an object")
        code = error.get("code")
        message = error.get("message")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or not isinstance(message, str)
            or not message
        ):
            raise PluginHostProtocolError("Plugin Host error has invalid code or message")
    return value


__all__ = [
    "PLUGIN_HOST_PROTOCOL_VERSION",
    "make_error",
    "make_request",
    "make_result",
    "validate_request",
    "validate_response",
]
