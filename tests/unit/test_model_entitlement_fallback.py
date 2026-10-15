"""A model-specific 403 advances; invalid credentials and refusals stop."""

import httpx
import pytest

from ai_orchestrator.domain.errors import ModelUnavailable, PolicyDenied, ValidationError
from ai_orchestrator.models.gateway import ModelCandidate, ModelGateway, ModelProfile, ModelRequest
from ai_orchestrator.models.providers import OpenAICompatibleProvider


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (403, {"error": {"message": "only available on agentic harnesses"}}, None),
        (401, {"error": {"message": "invalid key"}}, ValidationError),
        (403, {"error": {"message": "Content filter triggered."}}, PolicyDenied),
    ],
)
async def test_entitlement_fallback_is_bounded_and_does_not_bypass_refusals(status, body, expected):
    models = []

    def handle(request):
        import json

        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "first":
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    provider = OpenAICompatibleProvider(
        "fixture", api_key="test", base_url="https://example.invalid/v1", max_retries=2
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handle)
    )
    profile = ModelProfile(
        name="test",
        candidates=tuple(ModelCandidate(provider="fixture", model=m) for m in ("first", "second")),
    )
    gw = ModelGateway(providers={"fixture": provider}, profiles={"test": profile})
    try:
        if expected:
            with pytest.raises(expected):
                await gw.complete(ModelRequest(profile="test"))
            assert models == ["first"]
        else:
            response = await gw.complete(ModelRequest(profile="test"))
            assert models == ["first", "second"]
            assert response.decision.routing_reason == "fallback"
    finally:
        await gw.aclose()


async def test_unknown_model_404_is_not_retried_at_provider():
    calls = []
    provider = OpenAICompatibleProvider(
        "fixture", api_key="test", base_url="https://example.invalid/v1", max_retries=2
    )

    def handle(request):
        calls.append(request)
        return httpx.Response(404, json={"error": {"message": "unknown model"}})

    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ModelUnavailable):
            await provider.complete(
                ModelCandidate(provider="fixture", model="removed"), ModelRequest(profile="test")
            )
        assert len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"choices": {"bad": "shape"}},
        {"choices": [{"message": {"content": ["invalid"]}}]},
        {"choices": [{"message": {"tool_calls": ["invalid"]}}]},
    ],
)
async def test_malformed_success_responses_are_model_failures(body):
    provider = OpenAICompatibleProvider(
        "fixture",
        api_key="test",
        base_url="https://example.invalid/v1",
        max_retries=0,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(ModelUnavailable):
            await provider.complete(
                ModelCandidate(provider="fixture", model="broken"), ModelRequest(profile="test")
            )
    finally:
        await provider.aclose()
