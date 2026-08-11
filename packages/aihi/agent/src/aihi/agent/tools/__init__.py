"""Tool contracts, registry, and built-ins.

The dispatcher is imported lazily because low-level consumers such as Policy
need ``tools.spec`` without importing the policy-aware execution layer back.
"""

from typing import TYPE_CHECKING, Any

from aihi.agent.tools.base import Tool, ToolContext, ToolExecutionResult
from aihi.agent.tools.registry import ToolRegistry
from aihi.agent.tools.spec import IdempotencyPolicy, ToolSpec

if TYPE_CHECKING:
    from aihi.agent.tools.dispatcher import DispatchResult, ToolDispatcher


def __getattr__(name: str) -> Any:
    if name in {"DispatchResult", "ToolDispatcher"}:
        from aihi.agent.tools.dispatcher import DispatchResult, ToolDispatcher

        return {"DispatchResult": DispatchResult, "ToolDispatcher": ToolDispatcher}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "DispatchResult",
    "IdempotencyPolicy",
    "Tool",
    "ToolContext",
    "ToolDispatcher",
    "ToolRegistry",
    "ToolExecutionResult",
    "ToolSpec",
]
