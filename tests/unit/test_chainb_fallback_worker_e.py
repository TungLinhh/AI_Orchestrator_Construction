"""Worker-E (chain-B fallback): DOTS-first failover, 403 routing, no blind retry.

Offline only (`httpx.MockTransport`). No network traffic, no model calls,
no credentials. Covers the chain-5 shape: DOTS answers HTTP 200 with
`reasoning_tokens (1006) > completion_tokens (949)`, which the strict parser
must keep rejecting, and the gateway must then fail over instead of hanging
or blindly re-posting the identical request.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from tenacity import wait_none

from ai_orchestrator.domain.errors import ModelUnavailable, ValidationError
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelProfile,
    ModelRequest,
)
from ai_orchestrator.models.providers import OpenAICompatibleProvider, _parse_openai_response

DOTS = "dots-studio/dots-3-note-preview:free"
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free"


def _dots_anomalous_body() -> dict:
    """chain-round-5 observer[0] numbers: prompt=1446, completion=949, reasoning=1006."""
    return {
        "choices": [{"message": {"content": "dots answer"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1446,
            "completion_tokens": 949,
            "total_tokens": 2395,
            "completion_tokens_details": {"reasoning_tokens": 1006},
        },
    }


def _ok_body(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }


def _provider(handle, *, max_retries: int = 0) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        "openrouter",
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        max_retries=max_retries,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handle),
    )
    return provider


def _gateway(provider: OpenAICompatibleProvider, models: tuple[str, ...]) -> ModelGateway:
    return ModelGateway(
        providers={"openrouter": provider},
        profiles={
            "primary": ModelProfile(
                name="primary",
                requires_structured_output=False,
                max_latency_ms=60_000,
                max_retries=2,
                candidates=tuple(
                    ModelCandidate(provider="openrouter", model=name) for name in models
                ),
            )
        },
    )


def test_strict_parser_still_rejects_chain5_numbers() -> None:
    """The strict invariant is untouched: reasoning > completion is refused, never clamped."""
    candidate = ModelCandidate(provider="openrouter", model=DOTS)
    with pytest.raises(ModelUnavailable, match="inconsistent completion/reasoning"):
        _parse_openai_response(_dots_anomalous_body(), candidate, 1)


async def test_dots_anomalous_usage_fails_over_without_hanging() -> None:
    """DOTS parser-reject -> next candidate serves; failover takes milliseconds offline."""
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if "dots" in model:
            return httpx.Response(200, json=_dots_anomalous_body())
        return httpx.Response(200, json=_ok_body("nvidia answer"))

    gateway = _gateway(_provider(handle), (DOTS, NEMOTRON))
    try:
        started = time.monotonic()
        response = await gateway.complete(ModelRequest(profile="primary", prompt="hi"))
        elapsed = time.monotonic() - started
    finally:
        await gateway.aclose()
    assert response.model_used == NEMOTRON
    assert response.decision is not None and response.decision.routing_reason == "fallback"
    assert calls == [DOTS, NEMOTRON]
    assert elapsed < 5.0


async def test_403_entitlement_fails_fast_and_routing_continues(monkeypatch) -> None:
    """Plain 403 entitlement: exactly one POST (no retry even with budget) then fallback."""

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if "inkling" in model:
            return httpx.Response(
                403,
                json={"error": {"message": "This model requires an entitlement.", "code": 403}},
            )
        return httpx.Response(200, json=_ok_body("fallback answer"))

    provider = _provider(handle, max_retries=2)
    monkeypatch.setattr(provider.complete.retry, "wait", wait_none())
    gateway = _gateway(provider, ("inkling-small:free", "other-model:free"))
    try:
        response = await gateway.complete(ModelRequest(profile="primary", prompt="hi"))
    finally:
        await gateway.aclose()
    assert response.model_used == "other-model:free"
    assert response.decision is not None and response.decision.routing_reason == "fallback"
    assert calls == ["inkling-small:free", "other-model:free"]


async def test_parser_rejection_is_not_retried_before_failover(monkeypatch) -> None:
    """A locally refused body must not be re-posted: one DOTS POST, then the fallback."""

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if "dots" in model:
            return httpx.Response(200, json=_dots_anomalous_body())
        return httpx.Response(200, json=_ok_body("fallback answer"))

    provider = _provider(handle, max_retries=2)
    monkeypatch.setattr(provider.complete.retry, "wait", wait_none())
    gateway = _gateway(provider, (DOTS, "other-model:free"))
    try:
        response = await gateway.complete(ModelRequest(profile="primary", prompt="hi"))
    finally:
        await gateway.aclose()
    assert response.model_used == "other-model:free"
    assert calls.count(DOTS) == 1
    assert calls == [DOTS, "other-model:free"]


async def test_transient_503_is_still_retried_then_fails_over(monkeypatch) -> None:
    """Genuinely transient faults keep their retry budget; only local rejects skip it."""

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "flaky-model:free":
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=_ok_body("fallback answer"))

    provider = _provider(handle, max_retries=1)
    monkeypatch.setattr(provider.complete.retry, "wait", wait_none())
    gateway = _gateway(provider, ("flaky-model:free", "other-model:free"))
    try:
        response = await gateway.complete(ModelRequest(profile="primary", prompt="hi"))
    finally:
        await gateway.aclose()
    assert response.model_used == "other-model:free"
    assert calls == ["flaky-model:free", "flaky-model:free", "other-model:free"]


async def test_budget_403_is_terminal_and_does_not_fall_back() -> None:
    """A 403 carrying a budget limit_source refuses the request itself: no next model."""

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "$8 of $10 used; resets tomorrow.",
                    "code": 403,
                    "metadata": {"limit_source": "openrouter_key_limit"},
                }
            },
        )

    gateway = _gateway(_provider(handle, max_retries=2), ("first-model:free", "second-model:free"))
    try:
        with pytest.raises(ValidationError, match="budget limit exhausted"):
            await gateway.complete(ModelRequest(profile="primary", prompt="hi"))
    finally:
        await gateway.aclose()
    assert calls == ["first-model:free"]
