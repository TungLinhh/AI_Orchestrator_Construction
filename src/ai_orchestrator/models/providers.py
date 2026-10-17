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
import re
import time
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
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
    details = getattr(exc, "details", {}) or {}
    if details.get("status") in {400, 401, 403, 404, 422}:
        return False
    if details.get("retryable") is False:
        # A locally rejected response (e.g. inconsistent usage counts): the
        # provider already answered and we refused it, so re-sending the
        # identical request cannot help. Fail over instead of re-posting.
        return False
    return isinstance(
        exc,
        ModelRateLimited
        | ModelUnavailable
        | ExternalServiceError
        | httpx.TransportError
        | httpx.HTTPStatusError,
    )


def _retry_limit_reached(state: RetryCallState) -> bool:
    provider = state.args[0]
    request = state.kwargs.get("request") or state.args[2]
    retries = provider._max_retries
    if request.max_provider_retries is not None:
        retries = min(retries, max(0, request.max_provider_retries))
    return bool(state.attempt_number >= max(0, retries) + 1)


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
        stop=_retry_limit_reached,
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
        except asyncio.CancelledError:
            # Cancellation is BaseException: it must never be converted into a
            # model error. Log model+elapsed for telemetry, then re-raise.
            # Cost stays unknown, never zero-filled.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "model request cancelled",
                model=candidate.key(),
                elapsed_ms=elapsed_ms,
            )
            raise
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
        if response.status_code == 403:
            body = _safe_body(response)
            if _content_filter_refusal(body) is not None:
                raise PolicyDenied(
                    f"{candidate.key()}: the provider's content filter refused this prompt",
                    details={"model": candidate.key(), "body": _safe_dict(body)},
                )
            # Model-specific entitlement can differ between candidates on the
            # same key. Inkling Small currently requires a registered agentic
            # harness. Do not retry or disguise the client; try the next model.
            if _budget_denied(body):
                # A budget denial is terminal, never fallback-eligible: trying
                # the next model would spend past the key/workspace/guardrail
                # budget the denial protects (OpenRouter surfaces the 402
                # budget family as HTTP 403 with error.metadata.limit_source
                # or a "$X of $Y used; resets ..." message).
                error = _as_error(body)
                raise ValidationError(
                    f"{candidate.key()}: budget limit exhausted (403): "
                    + str(error.get("message", "budget exhausted"))[:300],
                    details={"model": candidate.key(), "status": 403},
                )
            if _guardrail_denied(body):
                # A guardrail/permission denial (non-content-filter variant) is
                # a refusal of this request, not a model fault: no other model
                # may serve it either.
                raise PolicyDenied(
                    f"{candidate.key()}: provider guardrail denied this request (403)",
                    details={"model": candidate.key(), "body": _safe_dict(body)},
                )
            # Upstream error shapes vary (dict, string, null): _as_error
            # normalises them, so a malformed 403 reports a structured
            # ModelUnavailable instead of an internal AttributeError.
            error = _as_error(body)
            raise ModelUnavailable(
                f"{candidate.key()}: model access denied (403): "
                + str(error.get("message", "access denied"))[:300],
                details={"model": candidate.key(), "status": 403},
            )
        if response.status_code == 402:
            # Payment required is a budget denial by definition: terminal and
            # never fallback-eligible, for the same reason as a 403 budget body.
            body = _safe_body(response)
            error = _as_error(body)
            raise ValidationError(
                f"{candidate.key()}: budget limit exhausted (402): "
                + str(error.get("message", "payment required"))[:300],
                details={"model": candidate.key(), "status": 402},
            )
        if response.status_code == 401:
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
            raise ModelUnavailable(msg, details={"model": candidate.key(), "status": 404})
        if response.status_code == 400:
            body = _safe_body(response)
            error = _as_error(body if isinstance(body, dict) else {})
            details = _redacted_400_details(candidate, error)
            # The denial message is part of the exception text: use the
            # secret-scrubbed details message, never the raw provider string.
            msg = f"{candidate.key()}: request rejected (400): {details['message'] or 'unknown'}"
            raise ValidationError(msg, details=details)
        if response.status_code >= 400:
            msg = f"{candidate.key()}: provider returned {response.status_code}"
            raise ExternalServiceError(
                msg, details={"model": candidate.key(), "status": response.status_code}
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ModelUnavailable(f"{candidate.key()}: response is not JSON", cause=exc) from exc
        if not isinstance(body, dict):
            raise ModelUnavailable(f"{candidate.key()}: response must be an object")
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
        envelope = _error_envelope(body)
        if envelope is not None:
            # An upstream fault delivered inside HTTP 200 with no `choices` and
            # no `usage`. Measured 2026-10-16 on nemotron: HTTP 200 carrying
            # `{'error': {'code': 503, 'message': 'Upstream error from Nvidia:
            # Service temporarily overloaded'}}`. Reporting that as "response
            # contained no choices" hides the actual status and invites a retry
            # of a request that was never malformed. No usage is invented here:
            # the call failed, so its cost stays unknown and the gateway records
            # the fallback, not a fabricated ledger row.
            raise _error_envelope_error(candidate, envelope)

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
    first = (
        choices[0]
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if first is not None:
        finish_reason = str(first.get("finish_reason") or "")
        if finish_reason in {"content_filter", "refused"}:
            return finish_reason
        assistant_message = first.get("message")
        if isinstance(assistant_message, dict):
            refusal = assistant_message.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                return "refused"
    return None


#: OpenRouter `error.metadata.limit_source` values that name a budget, not a
#: model. A denial carrying one must never fall back to another model, which
#: would spend past the budget the denial protects.
#: https://openrouter.ai/docs/api_reference/errors-and-debugging
_BUDGET_LIMIT_SOURCES = frozenset(
    {"openrouter_guardrail_budget", "openrouter_workspace_budget", "openrouter_key_limit"}
)

#: `error.metadata.error_type` (or the error's own type field) naming the
#: 402-budget family surfaced as HTTP 403.
_BUDGET_ERROR_TYPES = frozenset({"payment_required"})

#: Guardrail/permission denial variants that are refusals of the request, not
#: model faults. Non-content-filter-string forms, so `_content_filter_refusal`
#: does not catch them; they still must not fall back.
_GUARDRAIL_ERROR_TYPES = frozenset({"permission_denied", "content_policy_violation", "refusal"})

#: The budget-exhausted message shape: "$8 of $10 used; resets ...".
_BUDGET_MESSAGE_RE = re.compile(
    r"\$\s*[\d.,]+\s+of\s+\$\s*[\d.,]+\s+used"
    r"|budget[^.]{0,60}(exceed|exhaust|deplet|limit)"
    r"|(exceed|exhaust|deplet)[^.]{0,60}budget",
    re.IGNORECASE,
)


#: Secret-shaped text that must never survive into shared errors or audit rows,
#: even inside a provider message. Values are replaced, field names kept so the
#: shape of the denial stays diagnosable.
_SECRET_TEXT_RES = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=_-]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|authorization|client[_-]?secret|secret|passwd|credential)\b"
        r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
    ),
)


def _scrub_secrets(text: str) -> str:
    """Replace credential-shaped substrings with [redacted]."""
    scrubbed = str(text or "")
    for pattern in _SECRET_TEXT_RES:
        scrubbed = pattern.sub(
            lambda match: (
                match.group(0)[: match.group(0).find(" ") + 1] + "[redacted]"
                if " " in match.group(0)
                else "[redacted]"
            ),
            scrubbed,
        )
    return scrubbed


def _scalar_or_none(value: Any, *, cap: int = 200) -> str | None:
    """A whitelisted operational value: scalar-coerced, capped, never a container."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value)
    return text[:cap] if text else None


def _redacted_400_details(
    candidate: ModelCandidate, error: dict[str, Any], *, status: int = 400
) -> dict[str, Any]:
    """Whitelisted denial metadata: operational fields only, no opaque raw.

    Only known-safe scalar fields are kept (`error_type`, `limit_source`,
    `provider_code` from `metadata.provider_code`, `status`, plus a
    secret-scrubbed message prefix). The provider's opaque `raw`/echo
    containers (`request`, `messages`, `headers`, nested metadata) are never
    serialized into shared errors or audit rows; full bodies stay in private
    synthetic replay evidence. Truncation alone is not redaction.
    """
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "model": candidate.key(),
        "status": status,
        "error_type": _scalar_or_none(
            metadata.get("error_type") or error.get("error_type") or error.get("type")
        ),
        "limit_source": _scalar_or_none(metadata.get("limit_source")),
        "provider_code": _scalar_or_none(metadata.get("provider_code")),
        "message": _scrub_secrets(str(error.get("message") or ""))[:300],
    }


def _as_error(body: dict[str, Any]) -> dict[str, Any]:
    """The inner error dict, whether `body` is a full response or bare error."""
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    if "choices" not in body and any(
        key in body for key in ("code", "message", "metadata", "error_type", "type")
    ):
        return body
    return {}


def _budget_denied(body: dict[str, Any]) -> bool:
    """Whether this 403-shaped denial is really a budget denial (terminal)."""
    error = _as_error(body)
    if not error:
        return False
    try:
        if int(str(error.get("code") or "").strip()) == 402:
            return True
    except TypeError, ValueError:
        pass
    metadata = error.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    if str(metadata.get("limit_source") or "").strip() in _BUDGET_LIMIT_SOURCES:
        return True
    for error_type in (metadata.get("error_type"), error.get("error_type"), error.get("type")):
        if str(error_type or "").strip().lower() in _BUDGET_ERROR_TYPES:
            return True
    return bool(_BUDGET_MESSAGE_RE.search(str(error.get("message") or "")))


def _guardrail_denied(body: dict[str, Any]) -> bool:
    """Whether this 403-shaped denial is a guardrail/permission refusal (terminal)."""
    error = _as_error(body)
    if not error:
        return False
    metadata = error.get("metadata")
    candidates: list[Any] = [error.get("error_type"), error.get("type")]
    if isinstance(metadata, dict):
        if metadata.get("patterns"):
            return True
        candidates.append(metadata.get("error_type"))
    return any(str(item or "").strip().lower() in _GUARDRAIL_ERROR_TYPES for item in candidates)


def _safe_dict(body: dict[str, Any]) -> dict[str, Any]:
    """A provider body, truncated so a diagnostic cannot become a payload."""
    return {"error": body.get("error"), "model": body.get("model")}


def _error_envelope(body: dict[str, Any]) -> dict[str, Any] | None:
    """An upstream fault delivered as HTTP 200 with no usable completion.

    Some OpenRouter upstreams report overloads as `{"error": {...}, "id": ...}`
    with status 200 and no `choices`/`usage`. A body that carries `choices` is
    an answer (possibly with warnings) and is parsed normally; only a choiceless
    body with an `error` payload counts as an envelope.
    """
    error = body.get("error")
    if error is None or body.get("choices"):
        return None
    if isinstance(error, dict):
        return error
    return {"message": str(error)[:500]}


def _error_envelope_error(candidate: ModelCandidate, error: dict[str, Any]) -> BaseException:
    """Classify a 200-envelope fault the way the matching HTTP status is classified.

    Upstream overloads and rate limits stay retryable/fallback-eligible
    (`ModelUnavailable`/`ModelRateLimited`, bounded by the caller's retry budget
    and the gateway's candidate list). Credential failures stay terminal
    (`ValidationError`, never retried). Budget denials (402, or 403 carrying a
    budget `limit_source`/`payment_required`/exhausted message) stay terminal
    (`ValidationError`): falling back would spend past the budget. Guardrail or
    permission denials (403 with a refusal-shaped `error_type` or guardrail
    `patterns`) stay terminal (`PolicyDenied`). Content-filter refusals never
    reach here: `_content_filter_refusal` runs first and stays terminal. No usage or cost is
    attached — a failed call's cost is unknown, never zero-filled.
    """
    message = str(error.get("message") or "unknown upstream error")[:300]
    try:
        status = int(str(error.get("code") or "").strip())
    except TypeError, ValueError:
        status = 0
    lowered = message.lower()
    if status == 401 or ("invalid" in lowered and "key" in lowered) or "unauthorized" in lowered:
        return ValidationError(
            f"{candidate.key()}: authentication or authorisation failed (upstream 401)",
            details={"model": candidate.key(), "status": 401},
        )
    if status == 429 or "rate limit" in lowered or "throttle" in lowered:
        return ModelRateLimited(
            f"{candidate.key()}: rate limited (upstream 429)",
            details={"model": candidate.key(), "status": 429},
        )
    if status == 400:
        # A malformed request inside the envelope carries the same
        # whitelisted 400 telemetry as the HTTP 400 path. The message in the
        # exception text is the secret-scrubbed details message.
        details = _redacted_400_details(candidate, error, status=400)
        return ValidationError(
            f"{candidate.key()}: request rejected (upstream 400): "
            f"{details['message'] or 'unknown upstream error'}",
            details=details,
        )
    if status == 402 or (status == 403 and _budget_denied(error)):
        # A budget denial inside the envelope is terminal for the same reason
        # as an HTTP 403 budget body: falling back would spend past the budget.
        return ValidationError(
            f"{candidate.key()}: budget limit exhausted (upstream {status}): {message}",
            details={"model": candidate.key(), "status": status},
        )
    if status == 403 and _guardrail_denied(error):
        # A guardrail/permission denial is a refusal of the request, not a
        # model fault, so no other candidate may serve it either.
        return PolicyDenied(
            f"{candidate.key()}: provider guardrail denied this request (upstream 403)",
            details={"model": candidate.key(), "body": _safe_dict({"error": error})},
        )
    shown = status if status else "unknown"
    return ModelUnavailable(
        f"{candidate.key()}: upstream provider error (status {shown}): {message}",
        details={"model": candidate.key(), "status": status or None},
    )


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
    if not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise ModelUnavailable(f"{candidate.key()}: malformed response choices")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict) or not isinstance(message.get("content") or "", str):
        raise ModelUnavailable(f"{candidate.key()}: malformed assistant message")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or any(
        not isinstance(call, dict) or not isinstance(call.get("function"), dict) for call in calls
    ):
        raise ModelUnavailable(f"{candidate.key()}: malformed tool calls")

    raw_usage = body.get("usage") or {}
    details = raw_usage.get("completion_tokens_details") or {}
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    completion_tokens = int(raw_usage.get("completion_tokens") or 0)
    reasoning_tokens = int(details.get("reasoning_tokens") or 0)
    if not 0 <= reasoning_tokens <= completion_tokens:
        raise ModelUnavailable(
            f"{candidate.key()}: inconsistent completion/reasoning usage",
            details={
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                # Local verdict, not a transport fault: retrying the identical
                # request cannot change the provider's miscount, so the gateway
                # must fail over instead of re-posting (see `_should_retry`).
                "retryable": False,
            },
        )
    usage = TokenUsage(
        input_tokens=int(raw_usage.get("prompt_tokens") or 0),
        # OpenRouter's completion count includes reasoning; our internal buckets
        # are additive. Preserve raw_usage, but count each completion token once.
        output_tokens=completion_tokens - reasoning_tokens,
        reasoning_tokens=reasoning_tokens,
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
