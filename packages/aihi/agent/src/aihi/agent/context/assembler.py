"""Pure model-input assembly.

Assembly owns prompt ordering and large Tool Result materialization. It does
not decide when to compact and it never writes a Session event. This makes a
rebuild after compaction or a provider retry deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from aihi.agent.artifacts import ArtifactPolicy, ArtifactRef, ArtifactStore
from aihi.agent.context.models import AssembledContext, ContextBudget, ContextSection
from aihi.models import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    estimate_messages_tokens,
    estimate_text_tokens,
)


class ContextAssembler:
    """Materialize a stable-prefix + dynamic-suffix model request projection."""

    def __init__(
        self,
        *,
        artifact_threshold_tokens: int = 1_024,
        artifact_preview_chars: int = 4_000,
    ) -> None:
        if artifact_threshold_tokens <= 0 or artifact_preview_chars <= 0:
            raise ValueError("Artifact thresholds must be positive")
        self.artifact_threshold_tokens = artifact_threshold_tokens
        self.artifact_preview_chars = artifact_preview_chars

    def assemble(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        system_prompt: str,
        budget: ContextBudget,
        sections: tuple[ContextSection, ...] = (),
        artifact_store: ArtifactStore | None = None,
        artifact_policy: ArtifactPolicy | None = None,
        known_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> AssembledContext:
        system_blocks = compose_system_blocks(system_prompt, sections)
        rendered_prompt = "\n\n".join(block.text for block in system_blocks)
        materialized, artifacts = self.materialize(
            tuple(messages),
            artifact_store=artifact_store,
            artifact_policy=artifact_policy,
            known_artifacts=known_artifacts,
        )
        estimated = (
            estimate_text_tokens(rendered_prompt)
            + budget.tool_schema_tokens
            + estimate_messages_tokens(materialized)
        )
        return AssembledContext(
            system_prompt=rendered_prompt,
            system_blocks=system_blocks,
            messages=materialized,
            artifacts=artifacts,
            estimated_tokens=estimated,
            budget=budget,
        )

    def materialize(
        self,
        messages: tuple[Message, ...] | list[Message],
        *,
        artifact_store: ArtifactStore | None,
        artifact_policy: ArtifactPolicy | None,
        known_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> tuple[tuple[Message, ...], tuple[ArtifactRef, ...]]:
        by_call = {
            str(ref.metadata["tool_call_id"]): ref
            for ref in known_artifacts
            if isinstance(ref.metadata.get("tool_call_id"), str)
        }
        artifacts: dict[str, ArtifactRef] = {ref.artifact_id: ref for ref in known_artifacts}
        materialized: list[Message] = []
        for message in messages:
            blocks: list[ContentBlock] = []
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    blocks.append(block)
                    continue
                ref = self._existing_ref(block, by_call, artifacts)
                is_large = (
                    block.metadata.get("context_projection") != "truncated"
                    and estimate_text_tokens(block.content) > self.artifact_threshold_tokens
                )
                if (
                    ref is None
                    and is_large
                    and artifact_store is not None
                ):
                    metadata = {"tool_call_id": block.tool_call_id, "is_error": block.is_error}
                    ref = (
                        artifact_store.put_text(block.content, metadata=metadata)
                        if artifact_policy is None
                        else artifact_store.put_text(
                            block.content,
                            metadata=metadata,
                            policy=artifact_policy,
                        )
                    )
                    artifacts[ref.artifact_id] = ref
                    by_call[block.tool_call_id] = ref
                if ref is None:
                    if not is_large:
                        blocks.append(block)
                        continue
                    blocks.append(
                        replace(
                            block,
                            content=_bounded_preview(
                                block.content,
                                self.artifact_preview_chars,
                                marker="[Tool output truncated in the model projection; "
                                "the original Message Event is unchanged.]",
                            ),
                            metadata={
                                **block.metadata,
                                "context_projection": "truncated",
                                "original_sha256": sha256(
                                    block.content.encode("utf-8")
                                ).hexdigest(),
                                "original_size_bytes": len(block.content.encode("utf-8")),
                            },
                        )
                    )
                    continue
                blocks.append(
                    replace(
                        block,
                        content=_bounded_preview(
                            block.content,
                            self.artifact_preview_chars,
                            marker="[Full tool output stored as an artifact.]",
                        ),
                        metadata={
                            **block.metadata,
                            "artifact_id": ref.artifact_id,
                            "artifact_sha256": ref.sha256,
                            "artifact_size_bytes": ref.size_bytes,
                        },
                    )
                )
            materialized.append(replace(message, content=tuple(blocks)))
        return tuple(materialized), tuple(artifacts.values())

    @staticmethod
    def _existing_ref(
        block: ToolResultBlock,
        by_call: dict[str, ArtifactRef],
        artifacts: dict[str, ArtifactRef],
    ) -> ArtifactRef | None:
        artifact_id = block.metadata.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id in artifacts:
            return artifacts[artifact_id]
        return by_call.get(block.tool_call_id)


def _bounded_preview(content: str, max_chars: int, *, marker: str) -> str:
    if len(content) <= max_chars:
        return content
    head_chars = max(1, int(max_chars * 0.70))
    tail_chars = max(0, max_chars - head_chars)
    tail = content[-tail_chars:] if tail_chars else ""
    return f"{content[:head_chars]}\n\n{marker}\n\n{tail}"


def compose_system_blocks(
    system_prompt: str,
    sections: tuple[ContextSection, ...] | list[ContextSection],
) -> tuple[TextBlock, ...]:
    """Keep the base system prompt stable and append dynamic sections."""

    blocks: list[TextBlock] = []
    if system_prompt.strip():
        blocks.append(TextBlock(system_prompt.strip(), stable_prefix=True))
    blocks.extend(
        TextBlock(section.render()) for section in sections if section.body.strip()
    )
    return tuple(blocks)


def compose_system_prompt(
    system_prompt: str,
    sections: tuple[ContextSection, ...] | list[ContextSection],
) -> str:
    return "\n\n".join(block.text for block in compose_system_blocks(system_prompt, sections))


__all__ = ["ContextAssembler", "compose_system_blocks", "compose_system_prompt"]
