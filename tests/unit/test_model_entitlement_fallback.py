"""A model-specific 403 advances; invalid credentials and refusals stop."""

import httpx
import pytest

from ai_orchestrator.domain.errors import (
    ModelRateLimited,
    ModelUnavailable,
    PolicyDenied,
    ValidationError,
)
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


def _envelope_provider(body: dict, *, max_retries: int = 0) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        "fixture",
        api_key="test",
        base_url="https://example.invalid/v1",
        max_retries=max_retries,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    return provider


async def test_http200_upstream_503_envelope_is_retryable_unavailable():
    """HTTP 200 carrying error.code 503 names the upstream instead of no-choices."""
    body = {"id": "gen-1", "error": {"code": 503, "message": "Upstream error: overloaded"}}
    provider = _envelope_provider(body)
    try:
        with pytest.raises(ModelUnavailable) as exc_info:
            await provider.complete(
                ModelCandidate(provider="fixture", model="busy"), ModelRequest(profile="test")
            )
        assert "upstream provider error (status 503)" in str(exc_info.value)
        assert exc_info.value.details.get("status") == 503
    finally:
        await provider.aclose()


async def test_http200_upstream_429_envelope_is_rate_limited():
    body = {"id": "gen-2", "error": {"code": 429, "message": "Rate limited"}}
    provider = _envelope_provider(body)
    try:
        with pytest.raises(ModelRateLimited):
            await provider.complete(
                ModelCandidate(provider="fixture", model="busy"), ModelRequest(profile="test")
            )
    finally:
        await provider.aclose()


async def test_http200_upstream_401_envelope_is_terminal():
    body = {"id": "gen-3", "error": {"code": 401, "message": "Unauthorized"}}
    provider = _envelope_provider(body)
    try:
        with pytest.raises(ValidationError):
            await provider.complete(
                ModelCandidate(provider="fixture", model="busy"), ModelRequest(profile="test")
            )
    finally:
        await provider.aclose()


async def test_http200_upstream_503_envelope_falls_back_to_next_candidate():
    """A 200-envelope fault on the primary must still reach the fallback."""
    models = []

    def handle(request):
        import json

        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "first":
            return httpx.Response(
                200, json={"error": {"code": 503, "message": "Upstream error: overloaded"}}
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    provider = OpenAICompatibleProvider(
        "fixture", api_key="test", base_url="https://example.invalid/v1", max_retries=0
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
        response = await gw.complete(ModelRequest(profile="test"))
        assert models == ["first", "second"]
        assert response.decision.routing_reason == "fallback"
    finally:
        await gw.aclose()


async def test_body_with_choices_and_error_warning_parses_normally():
    """An answer carrying choices is an answer, even with an error-shaped warning."""
    body = {
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        "error": {"code": 503, "message": "stale warning"},
    }
    provider = _envelope_provider(body)
    try:
        response = await provider.complete(
            ModelCandidate(provider="fixture", model="ok"), ModelRequest(profile="test")
        )
        assert response.text == "answer"
    finally:
        await provider.aclose()
