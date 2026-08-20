"""Deterministic cache-family identity for stable model request prefixes."""

from __future__ import annotations

import json
from hashlib import sha256

from aihi.models import ModelToolDefinition, TextBlock

PROMPT_CACHE_CONTRACT_VERSION = 1


def stable_system_blocks(blocks: tuple[TextBlock, ...]) -> tuple[TextBlock, ...]:
    """Return the one contiguous stable prefix declared by ``blocks``."""

    result: list[TextBlock] = []
    for block in blocks:
        if not block.stable_prefix:
            break
        result.append(block)
    return tuple(result)


def build_prompt_cache_key(
    *,
    provider_family: str,
    model: str,
    tools: tuple[ModelToolDefinition, ...],
    system_blocks: tuple[TextBlock, ...],
) -> str:
    """Hash only the canonical Provider/Model stable cache family."""

    canonical_tools = sorted(
        (tool.to_dict() for tool in tools),
        key=lambda item: str(item["name"]),
    )
    material = json.dumps(
        {
            "contract_version": PROMPT_CACHE_CONTRACT_VERSION,
            "provider_family": provider_family,
            "model": model,
            "tools": canonical_tools,
            "system_blocks": [block.text for block in stable_system_blocks(system_blocks)],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(material.encode("utf-8")).hexdigest()
    return f"aihi:prompt-cache:v{PROMPT_CACHE_CONTRACT_VERSION}:{digest}"


__all__ = [
    "PROMPT_CACHE_CONTRACT_VERSION",
    "build_prompt_cache_key",
    "stable_system_blocks",
]
