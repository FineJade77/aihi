"""Deterministic tool registry."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aihi.agent.tools.base import Tool
from aihi.agent.tools.spec import ToolSpec


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool[Any] | None:
        return self._tools.get(name)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)
