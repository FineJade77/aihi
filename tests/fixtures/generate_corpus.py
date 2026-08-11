"""Regenerate the frozen compatibility corpus from real runs.

Run this only when a writer-side change is intended, and review the diff: a
payload change is a compatibility decision, not a formatting one.

    python tests/fixtures/generate_corpus.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "packages" / "aihi" / "models" / "src"))
sys.path.insert(0, str(REPOSITORY / "packages" / "aihi" / "agent" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_builder import build_corpus  # noqa: E402


def main() -> None:
    target = Path(__file__).resolve().parent / "session_schema_v1.json"
    with tempfile.TemporaryDirectory() as workspace:
        document = asyncio.run(build_corpus(Path(workspace)))
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    events = sum(len(session["events"]) for session in document["sessions"])
    print(f"wrote {target} ({len(document['sessions'])} sessions, {events} events)")


if __name__ == "__main__":
    main()
