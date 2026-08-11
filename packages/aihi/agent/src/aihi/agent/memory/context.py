"""Read durable memory into a context, and propose new memory after a run.

The two directions stay asymmetric on purpose: reading is automatic, writing is
not. A finished run only ever produces `memory.candidate` proposals; promoting
one to a durable record still requires an explicit `MemoryService.write` with a
matching `MemoryAccess` (ARCHITECTURE §11).
"""

from __future__ import annotations

from collections.abc import Callable

from aihi.agent._core.events import Event
from aihi.agent.context import ContextSection
from aihi.agent.memory.errors import MemoryError as HarnessMemoryError
from aihi.agent.memory.service import MemoryService
from aihi.agent.memory.types import MemoryAccess, MemoryRecord, MemoryScope

_TITLE = "Relevant memory"


def render_records(records: tuple[MemoryRecord, ...]) -> str:
    return "\n".join(
        f"- [{record.kind.value}/{record.scope.value}] {record.content}" for record in records
    )


class MemoryContextContributor:
    """Retrieve scoped memory for the current request and render it read-only."""

    def __init__(
        self,
        service: MemoryService,
        access: MemoryAccess,
        *,
        scope: MemoryScope | None = None,
        limit: int = 5,
    ) -> None:
        if limit <= 0:
            raise ValueError("Memory context limit must be positive")
        self.service = service
        self.access = access
        self.scope = scope
        self.limit = limit

    def sections(self, request: object) -> tuple[ContextSection, ...]:
        query = str(getattr(request, "user_text", "") or "")
        if not query.strip():
            return ()
        scope_id = self._scope_id(request)
        records = self.service.retrieve(
            query,
            access=self.access,
            scope=self.scope,
            scope_id=scope_id,
            limit=self.limit,
        )
        body = render_records(records)
        if not body:
            return ()
        return (ContextSection(title=_TITLE, body=body, source="memory"),)

    def _scope_id(self, request: object) -> str | None:
        if self.scope == MemoryScope.SESSION:
            return str(getattr(request, "session_id", "") or "") or None
        if self.scope == MemoryScope.RUN:
            return str(getattr(request, "run_id", "") or "") or None
        return None


class MemoryCandidateRecorder:
    """Extract memory candidates from a finished run; never writes durably."""

    def __init__(
        self,
        service: MemoryService,
        *,
        scope: MemoryScope = MemoryScope.SESSION,
        source: str = "assistant",
    ) -> None:
        self.service = service
        self.scope = scope
        self.source = source

    def record(self, outcome: object, *, event_sink: Callable[[Event], object]) -> None:
        text = str(getattr(outcome, "assistant_text", "") or "")
        if not text.strip():
            return
        session_id = str(getattr(outcome, "session_id", "") or "")
        run_id = str(getattr(outcome, "run_id", "") or "")
        scope_id = run_id if self.scope == MemoryScope.RUN else session_id
        try:
            self.service.extract(
                text,
                source=self.source,
                scope=self.scope,
                scope_id=scope_id,
                session_id=session_id,
                run_id=run_id,
                event_sink=event_sink,
            )
        except HarnessMemoryError:
            # Memory is an optional side channel: a rejected candidate must not
            # change the outcome of a run that already succeeded.
            return


__all__ = ["MemoryCandidateRecorder", "MemoryContextContributor", "render_records"]
