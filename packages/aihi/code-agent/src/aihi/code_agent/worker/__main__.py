"""Process entry point for `python -m aihi.code_agent.worker`.

The CLI launches the Worker with exactly that command, so splitting `worker`
from a module into a package needs this file — a package is not directly
executable without it.
"""

from __future__ import annotations

import sys

from aihi.code_agent.worker.transport import main

if __name__ == "__main__":
    sys.exit(main())
