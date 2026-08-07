"""Register a trusted plugin's tools into a `ToolRegistry`.

Activation is deliberately not implicit. A host only starts after
`PluginTrustManager` has re-discovered and re-hashed the candidate and the
`PluginHostPolicy` has confirmed the plugin's declared capabilities and
permissions are a subset of the run's own. Registration then exposes the
resulting tools through the normal dispatcher path.
"""

from __future__ import annotations

from aiharness.core.types import ToolSpec
from aiharness.plugins.host import PluginHost, PluginRemoteTool
from aiharness.tools.registry import ToolRegistry


async def register_plugin_tools(
    registry: ToolRegistry,
    host: PluginHost,
    *,
    allowed_tools: frozenset[str] | None = None,
) -> tuple[ToolSpec, ...]:
    """Start the host if needed, then register each tool it advertises."""

    if not host.running:
        await host.start()
    definitions = await host.list_tools()
    registered: list[ToolSpec] = []
    for definition in definitions:
        if allowed_tools is not None and definition.name not in allowed_tools:
            continue
        tool = PluginRemoteTool(host=host, definition=definition)
        registry.register(tool)
        registered.append(tool.spec)
    return tuple(registered)


__all__ = ["register_plugin_tools"]
