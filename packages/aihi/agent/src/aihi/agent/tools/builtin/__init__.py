"""Built-in tools an application may register.

Read-only tools are concurrency-safe and need no approval; anything that writes
or executes goes through policy first.
"""

from aihi.agent.tools.builtin.bash import BashTool
from aihi.agent.tools.builtin.command import format_command_result
from aihi.agent.tools.builtin.edit_file import EditFileTool
from aihi.agent.tools.builtin.ledger import ReadLedger
from aihi.agent.tools.builtin.read_file import ReadFileTool
from aihi.agent.tools.builtin.search import GlobTool, GrepTool
from aihi.agent.tools.builtin.write_file import WriteFileTool

__all__ = [
    "BashTool",
    "EditFileTool",
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
    "ReadLedger",
    "WriteFileTool",
    "format_command_result",
]
