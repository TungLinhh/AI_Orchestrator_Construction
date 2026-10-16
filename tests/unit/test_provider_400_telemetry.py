"""HTTP 400 redacted telemetry + CancelledError propagation (offline, MockTransport)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelProfile,
    ModelRequest,
)
from ai_orchestrator.models.providers import OpenAICompatibleProvider


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


_CAND = ModelCandidate(provider="openrouter", model="dots-studio/dots-3-note-preview:free")
_REQ = ModelRequest(profile="p", prompt="hi")


async def test_http_400_details_carry_whitelisted_metadata_only() -> None:
    body = {
        "error": {
            "code": 400,
            "message": "invalid request params",
            "error_type": "fallback-type",
            "type": "fallback-legacy",
            "metadata": {
                "error_type": "invalid_request",
                "limit_source": "openrouter_key_limit",
                "provider_code": "atlas_invalid_params",
            },
        }
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    provider = _provider(handle)
    try:
        with pytest.raises(ValidationError) as exc_info:
            await provider.complete(_CAND, _REQ)
        assert "request rejected (400)" in str(exc_info.value)
        details = exc_info.value.details
        # Canonical error_type: metadata wins over error.error_type then error.type.
        assert details["error_type"] == "invalid_request"
        assert details["limit_source"] == "openrouter_key_limit"
        # provider_code comes from metadata.provider_code, never error.code.
        assert details["provider_code"] == "atlas_invalid_params"
        assert details["status"] == 400
        assert details["message"] == "invalid request params"
        # Whitelist only: no opaque raw, no body/headers/containers.
        assert set(details) == {
            "model",
            "status",
            "error_type",
            "limit_source",
            "provider_code",
            "message",
        }, details
    finally:
        await provider.aclose()


async def test_http_400_canonical_fallbacks() -> None:
    for error, expected in [
        ({"code": 400, "message": "m", "error_type": "mid"}, "mid"),
        ({"code": 400, "message": "m", "type": "legacy"}, "legacy"),
        ({"code": 400, "message": "m"}, None),
    ]:

        def handle(request: httpx.Request, _e=error) -> httpx.Response:
            return httpx.Response(400, json={"error": _e})

        provider = _provider(handle)
        try:
            with pytest.raises(ValidationError) as exc_info:
                await provider.complete(_CAND, _REQ)
            assert exc_info.value.details["error_type"] == expected
        finally:
            await provider.aclose()


async def test_http_400_message_capped_not_raw() -> None:
    long_message = "x" * 2000
    body = {"error": {"code": 400, "message": long_message}}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    provider = _provider(handle)
    try:
        with pytest.raises(ValidationError) as exc_info:
            await provider.complete(_CAND, _REQ)
        details = exc_info.value.details
        assert details["message"] == "x" * 300
        assert "raw" not in details
    finally:
        await provider.aclose()


async def test_envelope_400_branch_carries_same_telemetry() -> None:
    body = {
        "error": {
            "code": 400,
            "message": "invalid request params",
            "metadata": {
                "error_type": "invalid_request",
                "limit_source": "s",
                "provider_code": "p",
            },
        }
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = _provider(handle)
    try:
        with pytest.raises(ValidationError) as exc_info:
            await provider.complete(_CAND, _REQ)
        assert "upstream 400" in str(exc_info.value)
        details = exc_info.value.details
        assert details["error_type"] == "invalid_request"
        assert details["limit_source"] == "s"
        assert details["provider_code"] == "p"
        assert "raw" not in details
    finally:
        await provider.aclose()


async def test_http_400_nested_secrets_never_reach_details() -> None:
    """A malicious nested fixture: sentinels must not appear in details."""
    sentinel_auth = "Bearer SENTINEL_AUTH_TOKEN_XYZ"
    sentinel_key = "sk-or-v1-SENTINELKEY1234567890"
    sentinel_secret = "topsecret-request-messages-payload-SECRET"
    body = {
        "error": {
            "code": 400,
            "message": f"denied {sentinel_key} for key {sentinel_auth}",
            "metadata": {
                "error_type": "invalid_request",
                "headers": {"authorization": sentinel_auth, "api_key": sentinel_key},
                "request": {"messages": [{"content": sentinel_secret}]},
                "raw": {"messages": [sentinel_secret], "api_key": sentinel_key},
            },
        }
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body)

    provider = _provider(handle)
    try:
        with pytest.raises(ValidationError) as exc_info:
            await provider.complete(_CAND, _REQ)
        import json as _json

        serialized = _json.dumps(exc_info.value.details, default=str) + str(exc_info.value)
        assert "SENTINEL_AUTH_TOKEN_XYZ" not in serialized
        assert "SENTINELKEY1234567890" not in serialized
        assert "topsecret-request-messages-payload-SECRET" not in serialized
        assert "sk-or-v1-" not in serialized
    finally:
        await provider.aclose()


@pytest.mark.parametrize("error_value", ["upstream string failure", None])
async def test_http_403_malformed_error_shape_is_structured_failure(
    error_value: object,
) -> None:
    """A 403 with error as string/null must not crash with AttributeError."""
    from ai_orchestrator.domain.errors import ModelUnavailable

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": error_value})

    provider = _provider(handle)
    try:
        with pytest.raises(ModelUnavailable, match="model access denied \\(403\\)"):
            await provider.complete(_CAND, _REQ)
    finally:
        await provider.aclose()


async def test_provider_cancelled_error_propagates_not_converted() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    provider = _provider(handle, max_retries=2)
    try:
        with pytest.raises(asyncio.CancelledError):
            await provider.complete(_CAND, _REQ)
    finally:
        await provider.aclose()


async def test_gateway_cancelled_error_propagates_not_unavailable() -> None:
    from ai_orchestrator.models.gateway import ModelProvider

    class _CancellingProvider(ModelProvider):
        name = "fixture"

        async def complete(self, candidate, request):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError()

    profile = ModelProfile(
        name="test",
        candidates=(ModelCandidate(provider="fixture", model="m"),),
    )
    gw = ModelGateway(providers={"fixture": _CancellingProvider()}, profiles={"test": profile})
    try:
        with pytest.raises(asyncio.CancelledError):
            await gw.complete(ModelRequest(profile="test"))
    finally:
        await gw.aclose()
