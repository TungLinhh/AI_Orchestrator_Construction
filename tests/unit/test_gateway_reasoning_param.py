"""Per-candidate OpenRouter `reasoning` request param (offline, MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from ai_orchestrator.domain.errors import ModelUnavailable, ValidationError
from ai_orchestrator.models.gateway import ModelCandidate, ModelRequest
from ai_orchestrator.models.providers import OpenAICompatibleProvider, _parse_openai_response


def _provider(handle, *, max_retries: int = 0) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        "fixture",
        api_key="test",
        base_url="https://example.invalid/v1",
        max_retries=max_retries,
    )
    provider._client = httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handle)
    )
    return provider


def test_default_candidate_sends_no_reasoning_key() -> None:
    candidate = ModelCandidate(provider="openrouter", model="nvidia/nemotron-3-ultra-550b:free")
    assert candidate.reasoning is None
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert "reasoning" not in payload


async def test_dots_candidate_sends_reasoning_disabled() -> None:
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _provider(handle)
    try:
        candidate = ModelCandidate(
            provider="openrouter",
            model="dots-studio/dots-3-note-preview:free",
            reasoning={"enabled": False},
        )
        request = ModelRequest(profile="p", prompt="hi")
        assert request.to_provider_payload(candidate)["reasoning"] == {"enabled": False}
        await provider.complete(candidate, request)
        assert seen and seen[0]["reasoning"] == {"enabled": False}
    finally:
        await provider.aclose()


def test_malformed_reasoning_rejected_before_network() -> None:
    with pytest.raises(ValidationError):
        ModelCandidate(
            provider="openrouter", model="m", reasoning={"effort": "high", "max_tokens": 512}
        )  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        ModelCandidate(provider="openrouter", model="m", reasoning="off")  # type: ignore[arg-type]


def test_parser_still_rejects_reasoning_above_completion() -> None:
    candidate = ModelCandidate(provider="openrouter", model="dots-studio/dots-3-note-preview:free")
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 64,
            "completion_tokens_details": {"reasoning_tokens": 72},
        },
    }
    with pytest.raises(ModelUnavailable, match="inconsistent completion/reasoning usage"):
        _parse_openai_response(body, candidate, 1)


DOTS = "dots-studio/dots-3-note-preview:free"
NEMOTRON_FULL = "nvidia/nemotron-3-ultra-550b-a55b:free"

_TOOLS = [{"type": "function", "function": {"name": "submit_evidence"}}]


def test_dots_tools_payload_transmits_explicit_reasoning_verbatim() -> None:
    # No silent rewrite: an explicit operator value is transmitted verbatim
    # even alongside tools (the provider answers truthfully, 400 included).
    candidate = ModelCandidate(provider="openrouter", model=DOTS, reasoning={"enabled": False})
    request = ModelRequest(profile="p", prompt="hi", tools=list(_TOOLS), tool_choice="required")
    payload = request.to_provider_payload(candidate)
    assert payload["reasoning"] == {"enabled": False}
    assert payload["tools"] == list(_TOOLS)


def test_dots_toolfree_payload_keeps_reasoning_disabled() -> None:
    candidate = ModelCandidate(provider="openrouter", model=DOTS, reasoning={"enabled": False})
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert payload["reasoning"] == {"enabled": False}


def test_nemotron_tools_payload_transmits_reasoning_verbatim() -> None:
    candidate = ModelCandidate(
        provider="openrouter", model=NEMOTRON_FULL, reasoning={"enabled": False}
    )
    request = ModelRequest(profile="p", prompt="hi", tools=list(_TOOLS), tool_choice="required")
    payload = request.to_provider_payload(candidate)
    assert payload["reasoning"] == {"enabled": False}
