"""Coding Agent domain runtime for AIHI."""

from aihi.code_agent.framing import FrameError, read_frame, write_frame
from aihi.code_agent.protocol import (
    COMMAND_DESCRIPTORS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    WorkerServer,
)
from aihi.code_agent.worker import main, serve_stdio

__all__ = [
    "COMMAND_DESCRIPTORS",
    "FrameError",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "WorkerServer",
    "main",
    "read_frame",
    "serve_stdio",
    "write_frame",
]
