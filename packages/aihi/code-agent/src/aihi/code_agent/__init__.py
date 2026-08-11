"""Coding Agent domain runtime for AIHI."""

from typing import Any

from aihi.code_agent.config import CodeAgentConfig, CodeAgentConfigError, load_config
from aihi.code_agent.framing import FrameError, read_frame, write_frame
from aihi.code_agent.protocol import (
    COMMAND_DESCRIPTORS,
    PROTOCOL_VERSION,
    SERVER_NAME,
)
from aihi.code_agent.runtime import CodeAgentRuntime


def __getattr__(name: str) -> Any:
    """Load the executable Worker module lazily so ``python -m`` stays quiet."""

    if name in {"WorkerServer", "main", "serve_stdio"}:
        from aihi.code_agent.worker import WorkerServer, main, serve_stdio

        if name == "WorkerServer":
            return WorkerServer
        return main if name == "main" else serve_stdio
    raise AttributeError(name)

__all__ = [
    "COMMAND_DESCRIPTORS",
    "CodeAgentConfig",
    "CodeAgentConfigError",
    "CodeAgentRuntime",
    "FrameError",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "WorkerServer",
    "main",
    "load_config",
    "read_frame",
    "serve_stdio",
    "write_frame",
]
