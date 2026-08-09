"""Compact a context with a dedicated model, falling back to the offline generator.

The compact model is the only network call inside context compilation, which is
why `SummaryGenerator.generate` is async (ADR-0029). Everything about this path
is defensive: the input is bounded before it is sent, the reply must parse into
the same `StructuredSummary` schema the offline generator produces, and any
failure falls back rather than failing the run — a run that cannot compact fails
outright, so a worse summary always beats no summary.

The fallback is recorded, not hidden: the resulting `CompactionRecord` says
`l2_model_fallback`, so a compact model that quietly never works is visible.
"""

from __future__ import annotations

import json
from typing import Any

from aiharness.context.summary import (
    DeterministicSummaryGenerator,
    StructuredSummary,
    SummaryRequest,
)
from aiharness.core.types import Message, ModelRequest
from aiharness.models.base import MessageEnd, Provider

STRATEGY_MODEL = "l2_model"
STRATEGY_FALLBACK = "l2_model_fallback"

_INSTRUCTIONS = """You compact an AI agent's conversation so work can continue.

Reply with one JSON object and nothing else. Use these keys, all optional except
objective; every value except objective is an array of short strings:

  objective        - one sentence: what the user is trying to achieve
  constraints      - rules the work must respect
  decisions        - choices already made, and why
  files_changed    - paths already created or modified
  verified_state   - what has been checked to work, and how
  open_questions   - unresolved issues
  next_steps       - what to do next

Preserve facts that cannot be recovered from the remaining messages. Do not
invent progress that did not happen."""


def _lines(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


class ModelSummaryGenerator:
    """Ask a compact model for a structured summary; degrade to offline on any fault."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        max_input_chars: int = 24_000,
        max_output_tokens: int = 1_024,
        timeout_seconds: float = 60.0,
        fallback: DeterministicSummaryGenerator | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("compact model must be a non-empty string")
        if max_input_chars <= 0 or max_output_tokens <= 0 or timeout_seconds <= 0:
            raise ValueError("compact generator bounds must be positive")
        self.provider = provider
        self.model = model.strip()
        self.max_input_chars = max_input_chars
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.fallback = fallback or DeterministicSummaryGenerator()

    async def generate(self, request: SummaryRequest) -> StructuredSummary:
        try:
            text = await self._ask(request)
            summary = self._parse(text, request)
        except Exception:  # noqa: BLE001 - any fault degrades, never fails the run.
            degraded = await self.fallback.generate(request)
            return StructuredSummary(**{**_as_kwargs(degraded), "strategy": STRATEGY_FALLBACK})
        return summary

    async def _ask(self, request: SummaryRequest) -> str:
        transcript = self._render(request)
        model_request = ModelRequest(
            model=self.model,
            messages=(Message.text("user", transcript),),
            system_prompt=_INSTRUCTIONS,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout_seconds,
        )
        response = None
        stream = self.provider.stream(model_request)
        try:
            async for chunk in stream:
                if isinstance(chunk, MessageEnd):
                    response = chunk.response
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if response is None:
            raise ValueError("compact model produced no response")
        return response.message.text_content

    def _render(self, request: SummaryRequest) -> str:
        """Bound the input before it is sent: compaction must not need compaction."""

        parts = [
            f"{message.role}: {message.text_content}"
            for message in request.omitted_messages
            if message.text_content.strip()
        ]
        body = "\n\n".join(parts)
        if len(body) > self.max_input_chars:
            # Keep the tail: recent turns carry the state that must survive.
            body = "[earlier turns omitted]\n" + body[-self.max_input_chars :]
        return body or "(no textual messages to summarize)"

    def _parse(self, text: str, request: SummaryRequest) -> StructuredSummary:
        payload = json.loads(_json_span(text))
        if not isinstance(payload, dict):
            raise ValueError("compact model did not return a JSON object")
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("compact model returned no objective")
        return StructuredSummary(
            strategy=STRATEGY_MODEL,
            objective=objective.strip(),
            constraints=_lines(payload.get("constraints")),
            decisions=_lines(payload.get("decisions")),
            files_changed=_lines(payload.get("files_changed")),
            verified_state=_lines(payload.get("verified_state")),
            open_questions=_lines(payload.get("open_questions")),
            next_steps=_lines(payload.get("next_steps")),
            artifacts=request.artifact_ids,
            omitted_message_count=len(request.omitted_messages),
        )


def _json_span(text: str) -> str:
    """Take the outermost JSON object, tolerating prose or fences around it."""

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("compact model returned no JSON object")
    return text[start : end + 1]


def _as_kwargs(summary: StructuredSummary) -> dict[str, Any]:
    return {
        "objective": summary.objective,
        "constraints": summary.constraints,
        "decisions": summary.decisions,
        "files_changed": summary.files_changed,
        "verified_state": summary.verified_state,
        "open_questions": summary.open_questions,
        "next_steps": summary.next_steps,
        "permission_mode": summary.permission_mode,
        "skills": summary.skills,
        "subagents": summary.subagents,
        "artifacts": summary.artifacts,
        "omitted_message_count": summary.omitted_message_count,
    }


__all__ = ["STRATEGY_FALLBACK", "STRATEGY_MODEL", "ModelSummaryGenerator"]
