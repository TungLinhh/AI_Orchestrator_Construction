import httpx
import pytest
from tenacity import wait_none

from ai_orchestrator.models.gateway import ModelCandidate, ModelGateway, ModelProfile, ModelRequest
from ai_orchestrator.models.providers import OpenAICompatibleProvider


@pytest.mark.parametrize(
    "configured,override,attempts", [(0, None, 1), (1, None, 2), (2, 0, 1), (0, 2, 1)]
)
async def test_transport_retry_limit_allows_gateway_fallback(
    configured, override, attempts, monkeypatch
):
    calls = []

    def respond(request):
        import json

        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "unresponsive":
            raise httpx.ReadTimeout("Injected timeout", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "actual fallback output"}, "finish_reason": "stop"}
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        "unit-provider",
        api_key="fixture",
        base_url="https://example.invalid",
        max_retries=configured,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(provider.complete.retry, "wait", wait_none())
    gateway = ModelGateway(
        providers={"unit-provider": provider},
        profiles={
            "bounded": ModelProfile(
                name="bounded",
                candidates=tuple(
                    ModelCandidate(provider="unit-provider", model=name)
                    for name in ("unresponsive", "available")
                ),
            )
        },
    )
    try:
        response = await gateway.complete(
            ModelRequest(profile="bounded", max_provider_retries=override)
        )
        assert calls == ["unresponsive"] * attempts + ["available"]
        assert response.model_used == "available" and response.decision.routing_reason == "fallback"
    finally:
        await gateway.aclose()
