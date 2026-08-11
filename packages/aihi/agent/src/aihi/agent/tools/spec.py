"""Tool definition and Agent-owned execution governance metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from aihi.models import JsonObject, ModelToolDefinition

IdempotencyPolicy: TypeAlias = Literal["none", "safe", "keyed"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A model-visible definition plus Agent-owned execution constraints."""

    model_definition: ModelToolDefinition
    concurrency_safe: bool
    mutates: bool
    required_capabilities: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    idempotency: IdempotencyPolicy = "none"

    @classmethod
    def define(
        cls,
        *,
        name: str,
        description: str,
        input_schema: JsonObject,
        concurrency_safe: bool,
        mutates: bool,
        required_capabilities: tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
        idempotency: IdempotencyPolicy = "none",
    ) -> ToolSpec:
        return cls(
            model_definition=ModelToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
            ),
            concurrency_safe=concurrency_safe,
            mutates=mutates,
            required_capabilities=required_capabilities,
            timeout_seconds=timeout_seconds,
            idempotency=idempotency,
        )

    @property
    def name(self) -> str:
        return self.model_definition.name

    @property
    def description(self) -> str:
        return self.model_definition.description

    @property
    def input_schema(self) -> JsonObject:
        return self.model_definition.input_schema

    def to_dict(self) -> JsonObject:
        """Return the stable Agent context projection.

        ``idempotency`` remains execution-only metadata. Omitting it here
        preserves the established context-budget and compaction boundary while
        Providers receive the narrower ``model_definition`` projection.
        """

        return {
            **self.model_definition.to_dict(),
            "concurrency_safe": self.concurrency_safe,
            "mutates": self.mutates,
            "required_capabilities": list(self.required_capabilities),
            "timeout_seconds": self.timeout_seconds,
        }
