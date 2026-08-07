"""Model provider contracts, adapters, and routing."""

from aiharness.models.base import Provider, StreamChunk
from aiharness.models.gateway import ModelGateway, ModelRouter
from aiharness.models.retry import RetryPolicy
from aiharness.models.roles import ROLE_COMPACT, ROLE_PRIMARY, ROLE_SUBAGENT, ModelRoles

__all__ = [
    "ROLE_COMPACT",
    "ROLE_PRIMARY",
    "ROLE_SUBAGENT",
    "ModelGateway",
    "ModelRoles",
    "ModelRouter",
    "Provider",
    "RetryPolicy",
    "StreamChunk",
]
