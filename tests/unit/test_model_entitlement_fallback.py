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


def _gateway_recording(responder, *, max_retries: int = 2):
    """Two-candidate gateway recording every model the transport saw."""
    models = []

    def handle(request):
        import json

        model = json.loads(request.content)["model"]
        models.append(model)
        return responder(model)

    provider = OpenAICompatibleProvider(
        "fixture",
        api_key="test",
        base_url="https://example.invalid/v1",
        max_retries=max_retries,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handle)
    )
    profile = ModelProfile(
        name="test",
        candidates=tuple(ModelCandidate(provider="fixture", model=m) for m in ("first", "second")),
    )
    return ModelGateway(providers={"fixture": provider}, profiles={"test": profile}), models


def _ok_response():
    return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})


@pytest.mark.parametrize(
    "body",
    [
        # Budget limit_source (402 family surfaced as 403).
        {
            "error": {
                "code": 403,
                "message": "$8 of $10 used; resets in 3 days.",
                "metadata": {"limit_source": "openrouter_key_limit"},
            }
        },
        # payment_required error_type surfaced as 403.
        {
            "error": {
                "code": 403,
                "message": "Budget exceeded for this key.",
                "metadata": {"error_type": "payment_required"},
            }
        },
        # Budget-exhausted message shape with no metadata at all.
        {"error": {"code": 403, "message": "You used $9.50 of $10.00 used; resets tomorrow."}},
        # Inner 402 code carried on an HTTP 403.
        {"error": {"code": 402, "message": "Payment required: budget exhausted."}},
    ],
)
async def test_403_budget_denial_is_terminal_never_falls_back(body):
    """A 403 budget denial raises ValidationError after one attempt, no fallback."""
    gw, models = _gateway_recording(
        lambda model: httpx.Response(403, json=body) if model == "first" else _ok_response()
    )
    try:
        with pytest.raises(ValidationError):
            await gw.complete(ModelRequest(profile="test"))
        assert models == ["first"]
    finally:
        await gw.aclose()


async def test_http402_is_terminal_never_falls_back():
    gw, models = _gateway_recording(
        lambda model: (
            httpx.Response(402, json={"error": {"message": "Payment required."}})
            if model == "first"
            else _ok_response()
        )
    )
    try:
        with pytest.raises(ValidationError):
            await gw.complete(ModelRequest(profile="test"))
        assert models == ["first"]
    finally:
        await gw.aclose()


@pytest.mark.parametrize(
    "body",
    [
        # Guardrail patterns match with a permission error_type.
        {
            "error": {
                "code": 403,
                "message": "Forbidden by guardrail.",
                "metadata": {"error_type": "permission_denied", "patterns": ["pii"]},
            }
        },
        # Content-policy violation variant (no "content filter" string).
        {
            "error": {
                "code": 403,
                "message": "Blocked by policy.",
                "metadata": {"error_type": "content_policy_violation"},
            }
        },
    ],
)
async def test_403_guardrail_denial_is_terminal_never_falls_back(body):
    """A 403 guardrail/permission denial raises PolicyDenied, no fallback."""
    gw, models = _gateway_recording(
        lambda model: httpx.Response(403, json=body) if model == "first" else _ok_response()
    )
    try:
        with pytest.raises(PolicyDenied):
            await gw.complete(ModelRequest(profile="test"))
        assert models == ["first"]
    finally:
        await gw.aclose()


async def test_403_entitlement_denial_still_falls_back():
    """A genuine model-entitlement 403 stays ModelUnavailable and advances."""
    body = {"error": {"code": 403, "message": "access denied: unknown model"}}
    gw, models = _gateway_recording(
        lambda model: httpx.Response(403, json=body) if model == "first" else _ok_response()
    )
    try:
        response = await gw.complete(ModelRequest(profile="test"))
        assert models == ["first", "second"]
        assert response.decision.routing_reason == "fallback"
    finally:
        await gw.aclose()


async def test_envelope_403_budget_is_terminal_never_falls_back():
    """HTTP 200 carrying a 403 budget error raises ValidationError, no fallback."""
    body = {
        "id": "gen-budget",
        "error": {
            "code": 403,
            "message": "$8 of $10 used; resets in 3 days.",
            "metadata": {"limit_source": "openrouter_workspace_budget"},
        },
    }
    gw, models = _gateway_recording(
        lambda model: httpx.Response(200, json=body) if model == "first" else _ok_response()
    )
    try:
        with pytest.raises(ValidationError):
            await gw.complete(ModelRequest(profile="test"))
        assert models == ["first"]
    finally:
        await gw.aclose()


async def test_envelope_402_is_terminal():
    body = {"id": "gen-402", "error": {"code": 402, "message": "Payment required."}}
    provider = _envelope_provider(body)
    try:
        with pytest.raises(ValidationError) as exc_info:
            await provider.complete(
                ModelCandidate(provider="fixture", model="broke"), ModelRequest(profile="test")
            )
        assert exc_info.value.details.get("status") == 402
    finally:
        await provider.aclose()


async def test_envelope_403_guardrail_is_terminal():
    body = {
        "id": "gen-guard",
        "error": {
            "code": 403,
            "message": "Forbidden by guardrail.",
            "metadata": {"error_type": "refusal", "patterns": ["pii"]},
        },
    }
    provider = _envelope_provider(body)
    try:
        with pytest.raises(PolicyDenied):
            await provider.complete(
                ModelCandidate(provider="fixture", model="blocked"), ModelRequest(profile="test")
            )
    finally:
        await provider.aclose()
