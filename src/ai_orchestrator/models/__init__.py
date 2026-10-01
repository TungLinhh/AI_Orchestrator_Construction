"""Model gateway: provider abstraction, routing, fallback and cost accounting."""

from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelProfile,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    RoutingDecision,
    cosine_similarity,
    estimate_request_cost,
    hash_embedding,
)
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import (
    DeterministicProvider,
    OpenAICompatibleProvider,
    build_providers_from_settings,
)

__all__ = [
    "DeterministicProvider",
    "ModelCandidate",
    "ModelGateway",
    "ModelProfile",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "OpenAICompatibleProvider",
    "RoutingDecision",
    "build_providers_from_settings",
    "cosine_similarity",
    "default_profiles",
    "estimate_request_cost",
    "hash_embedding",
]
