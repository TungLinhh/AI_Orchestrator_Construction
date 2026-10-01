"""Provider adapters.

Three adapters, one normalising function. Every provider is reached over HTTP
with an OpenAI-compatible request shape, which is what keeps a new provider to a
few lines rather than a new client, a new retry policy and a new usage parser.

Retry classification lives here rather than at each call site:

    429, 408, 5xx, connection errors  -> retryable, with backoff
    400, 401, 403, 404, 422           -> terminal, never retried

A blind retry of a 401 is a credential-stuffing pattern to the provider and an
outage amplifier here, so it is refused at the lowest level.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.errors import (
    ExternalServiceError,
    ModelRateLimited,
    ModelUnavailable,
    PolicyDenied,
    ToolUnavailable,
    ValidationError,
)
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Status codes worth another attempt. Everything else is a bug in the request
#: or a credential problem, and retrying either just wastes the budget.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _should_retry(exc: BaseException) -> bool:
    return isinstance(
        exc,
        ModelRateLimited
        | ModelUnavailable
        | ExternalServiceError
        | httpx.TransportError
        | httpx.HTTPStatusError,
    )


class OpenAICompatibleProvider(ModelProvider):
    """Works with OpenAI, OpenRouter, and anything else speaking that dialect.

    One class rather than three, because the differences are the base URL and the
    key. Splitting them would mean three copies of the retry classification and
    three chances for them to drift.
    """

    def __init__(
        self,
        name: str,
        *,
        api_key: str | None,
        base_url: str,
        default_model: str = "",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout_s=timeout_s, max_retries=max_retries)
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._extra_headers = extra_headers or {}
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"content-type": "application/json"}
            if self._api_key:
                headers["authorization"] = f"Bearer {self._api_key}"
            headers.update(self._extra_headers)
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout_s,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        retry=retry_if_exception(_should_retry),
        reraise=True,
    )
    async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse:
        payload = request.to_provider_payload(candidate)
        client = self._get_client()
        started = time.monotonic()

        try:
            response = await client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            msg = f"{candidate.key()}: request timed out after {self._timeout_s}s"
            raise ModelUnavailable(msg, details={"model": candidate.key()}, cause=exc) from exc
        except httpx.TransportError as exc:
            msg = f"{candidate.key()}: transport error: {type(exc).__name__}"
            raise ExternalServiceError(msg, details={"model": candidate.key()}, cause=exc) from exc

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            msg = f"{candidate.key()}: rate limited"
            raise ModelRateLimited(
                msg,
                details={
                    "model": candidate.key(),
                    "retry_after": retry_after,
                    "limit_remaining": response.headers.get("x-ratelimit-remaining-requests"),
                },
            )
        if response.status_code in {401, 403}:
            # Never retried. The credential is wrong or insufficient, and
            # repeating the request does not change either.
            msg = (
                f"{candidate.key()}: authentication or authorisation failed "
                f"({response.status_code})"
            )
            raise ValidationError(
                msg, details={"model": candidate.key(), "status": response.status_code}
            )
        if response.status_code == 404:
            msg = f"{candidate.key()}: model not found at this provider"
            raise ModelUnavailable(msg, details={"model": candidate.key()})
        if response.status_code == 400:
            body = _safe_body(response)
            msg = (
                f"{candidate.key()}: request rejected (400): "
                f"{body.get('error', {}).get('message', 'unknown')}"
            )
            raise ValidationError(msg, details={"model": candidate.key(), "body": body})
        if response.status_code >= 400:
            msg = f"{candidate.key()}: provider returned {response.status_code}"
            raise ExternalServiceError(
                msg, details={"model": candidate.key(), "status": response.status_code}
            )

        body = response.json()
        refusal = _content_filter_refusal(body)
        if refusal is not None:
            # A provider's content filter is a *refusal*, not a fault, and it must
            # not read as one. A live run surfaced it as a 500-word escaped JSON
            # blob in the task's `last_error`, with no status and no code, so an
            # operator could not tell a permanent refusal from a transient error
            # and could not decide whether a retry could ever help.
            #
            # Not retried and not a fallback candidate: the same prompt will be
            # refused by the same filter, and burning the profile's fallbacks on it
            # would turn one refusal into several. The raw body goes to `details`
            # for diagnosis and nowhere else.
            msg = f"{candidate.key()}: the provider's content filter refused this prompt"
            raise PolicyDenied(msg, details={"model": candidate.key(), "body": _safe_dict(body)})

        return _parse_openai_response(body, candidate, latency_ms)


def _content_filter_refusal(body: dict[str, Any]) -> str | None:
    """The provider's refusal reason, or `None` if it answered normally.

    OpenRouter reports this in three places depending on the upstream provider, so
    all three are checked: an `error` object, a `finish_reason` of
    `content_filter`, and a refusal-shaped message. Missing the variant means
    reporting a refusal as an empty completion, which is worse.
    """
    error = body.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "").lower()
        if "content_filter" in code or "content filter" in message:
            return code or "content_filter"
    choices = body.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else None
    if first is not None and str(first.get("finish_reason") or "") == "content_filter":
        return "content_filter"
    return None


def _safe_dict(body: dict[str, Any]) -> dict[str, Any]:
    """A provider body, truncated so a diagnostic cannot become a payload."""
    return {"error": body.get("error"), "model": body.get("model")}


def _safe_body(response: httpx.Response) -> dict[str, Any]:
    """Parse a provider error body without risking a huge or binary payload."""
    try:
        parsed = response.json()
    except json.JSONDecodeError, ValueError:
        return {"error": {"message": response.text[:500]}}
    return parsed if isinstance(parsed, dict) else {"error": {"message": str(parsed)[:500]}}


def _parse_openai_response(
    body: dict[str, Any], candidate: ModelCandidate, latency_ms: int
) -> ModelResponse:
    choices = body.get("choices") or []
    if not choices:
        msg = f"{candidate.key()}: response contained no choices"
        raise ModelUnavailable(msg, details={"model": candidate.key()})
    message = choices[0].get("message") or {}

    raw_usage = body.get("usage") or {}
    details = raw_usage.get("completion_tokens_details") or {}
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    usage = TokenUsage(
        input_tokens=int(raw_usage.get("prompt_tokens") or 0),
        output_tokens=int(raw_usage.get("completion_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        cache_read_tokens=int(prompt_details.get("cached_tokens") or 0),
    )

    return ModelResponse(
        text=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        usage=usage,
        cost_usd=_cost_from_body(raw_usage, candidate),
        model_used=body.get("model") or candidate.model,
        provider=candidate.provider,
        latency_ms=latency_ms,
        finish_reason=choices[0].get("finish_reason") or "",
        raw_usage=raw_usage,
    )


def _cost_from_body(raw_usage: dict[str, Any], candidate: ModelCandidate) -> Money:
    """Use the provider's own cost field when it supplies one.

    OpenRouter reports `usage.cost` in USD. Trusting it is better than our own
    arithmetic because a provider that changes its prices should not silently
    produce a different bill than the one it charges.
    """
    if "cost" in raw_usage:
        # A provider that reports a cost we cannot parse is not an error: the
        # local estimate below is a better answer than failing the call, and a
        # warning says the provider's format changed.
        try:
            return Money(str(raw_usage["cost"]))
        except Exception as exc:
            logger.warning("model.cost_unparseable", error=type(exc).__name__)
    return candidate.pricing.cost_of(
        TokenUsage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
        )
    )


class DeterministicProvider(ModelProvider):
    """Scripted responses for tests and CI.

    Canned output would make a test that only exercises the harness, so this
    provider *parses the request*: it echoes structured content when an output
    schema asks for it, and answers a tool call when the request advertises one.
    The point is that budget accounting, usage tracking, retry handling and the
    result contract all run for real, with zero network and zero cost.
    """

    name = "deterministic"

    def __init__(self, *, responses: dict[str, str] | None = None, latency_ms: int = 1) -> None:
        super().__init__(timeout_s=1.0, max_retries=0)
        self._responses = responses or {}
        self._latency_ms = latency_ms
        self.call_count = 0
        #: Every request this provider saw, for assertions in tests.
        self.calls: list[ModelRequest] = []

    def is_configured(self) -> bool:
        return True

    async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self.calls.append(request)
        if self._latency_ms:
            await asyncio.sleep(self._latency_ms / 1000)

        usage = TokenUsage(
            input_tokens=max(1, (len(request.prompt) + len(request.system_instructions)) // 4),
            output_tokens=max(1, len(request.prompt) // 4),
        )
        cost = candidate.pricing.cost_of(usage)

        tool_calls: list[dict[str, Any]] = []
        text = ""

        if request.output_schema is not None:
            text = json.dumps(_conform(request.output_schema, request))
        elif request.tools:
            # Advertise exactly one call to the first tool, so a pipeline test
            # exercises tool selection and gating rather than skipping it.
            tool_calls = [
                {
                    "id": f"call_{self.call_count}",
                    "type": "function",
                    "function": {
                        "name": request.tools[0].get("function", {}).get("name", "tool"),
                        "arguments": "{}",
                    },
                }
            ]
        else:
            text = self._responses.get(
                request.prompt, f"deterministic response for profile {request.profile}"
            )

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            parsed=json.loads(text) if text.startswith("{") else None,
            usage=usage,
            cost_usd=cost,
            model_used=candidate.model,
            provider=candidate.provider,
            latency_ms=self._latency_ms,
            finish_reason="tool_calls" if tool_calls else "stop",
        )


def _conform(schema: dict[str, Any], request: ModelRequest) -> dict[str, Any]:
    """Produce a minimal object satisfying the requested schema.

    Only handles the shapes an agent contract actually uses: objects with typed
    properties. A schema it does not understand yields an empty object rather
    than a wrong value, so a schema change surfaces as a validation failure in
    the caller instead of a plausible-looking wrong answer.
    """
    properties = schema.get("properties") or {}
    result: dict[str, Any] = {}
    for name, spec in properties.items():
        json_type = (spec or {}).get("type")
        if json_type == "string":
            result[name] = str(request.prompt)[:200] or name
        elif json_type == "integer":
            result[name] = 0
        elif json_type == "number":
            result[name] = 0.0
        elif json_type == "boolean":
            result[name] = True
        elif json_type == "array":
            result[name] = []
        elif json_type == "object":
            result[name] = {}
        else:
            result[name] = None
    return result


def build_providers_from_settings(settings: Any) -> dict[str, ModelProvider]:
    """Construct the provider adapters a settings object implies.

    Every provider is constructed even when its key is missing, so
    `is_configured()` is the single place that answers "can I use this". A
    missing provider that is silently not registered is a debugging nightmare;
    a registered provider that reports itself unconfigured is a one-line log.
    """
    providers: dict[str, ModelProvider] = {
        "openai": OpenAICompatibleProvider(
            "openai",
            api_key=settings.openai_api_key.get_secret_value() or None,
            base_url="https://api.openai.com/v1",
            timeout_s=settings.model_request_timeout_s,
            max_retries=settings.model_max_retries,
        ),
        "anthropic": _AnthropicProvider(settings),
        "openrouter": OpenAICompatibleProvider(
            "openrouter",
            api_key=settings.openrouter_api_key.get_secret_value() or None,
            base_url="https://openrouter.ai/api/v1",
            timeout_s=settings.model_request_timeout_s,
            max_retries=settings.model_max_retries,
        ),
        "google": _GoogleProvider(settings),
    }
    return providers


class _AnthropicProvider(ModelProvider):
    """Anthropic, via its OpenAI-compatible endpoint.

    Anthropic exposes an OpenAI-shaped surface, which means the request builder
    and usage parser are shared. If that endpoint is ever withdrawn, only this
    class needs to be rewritten.
    """

    name = "anthropic"

    def __init__(self, settings: Any) -> None:
        super().__init__(
            timeout_s=settings.model_request_timeout_s, max_retries=settings.model_max_retries
        )
        self._inner = OpenAICompatibleProvider(
            "anthropic",
            api_key=settings.anthropic_api_key.get_secret_value() or None,
            base_url="https://api.anthropic.com/v1",
            timeout_s=settings.model_request_timeout_s,
            max_retries=settings.model_max_retries,
        )

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse:
        return await self._inner.complete(candidate, request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _GoogleProvider(ModelProvider):
    """Google Gemini, via the OpenAI-compatible endpoint on Generative Language."""

    name = "google"

    def __init__(self, settings: Any) -> None:
        super().__init__(
            timeout_s=settings.model_request_timeout_s, max_retries=settings.model_max_retries
        )
        self._inner = OpenAICompatibleProvider(
            "google",
            api_key=settings.gemini_api_key.get_secret_value() or None,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            timeout_s=settings.model_request_timeout_s,
            max_retries=settings.model_max_retries,
        )

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse:
        return await self._inner.complete(candidate, request)

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = [
    "DeterministicProvider",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "ToolUnavailable",
    "build_providers_from_settings",
]
