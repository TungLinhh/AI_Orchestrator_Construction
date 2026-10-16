"""Model gateway.

Agents reference a `model_profile` — `reasoning_high`, `fast_general`,
`cheap_general` — never a concrete model id. Swapping the provider behind a
profile is then a data change, not a code change, and no agent definition has to
be edited.

Routing decisions, in order:

  1. privacy. A provider the policy does not allow for this data classification
     is removed from the candidate list. This is a filter, not a preference: a
     restricted document must never reach an unapproved provider, and no amount
     of cost or latency preference may override that.
  2. capability. A profile that requires tool calling will not take a candidate
     that cannot do it.
  3. budget. A candidate whose estimated cost exceeds the remaining envelope is
     removed before the call, not after.
  4. fallback order. The first candidate that works, wins.

The failure path matters as much as the success path. A primary that fails
produces a *recorded* fallback with a reason, because "it worked" and "it worked
via a fallback" are different results for an evaluation and for a cost report.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from ai_orchestrator.domain.budget import ModelPricing, Money, TokenUsage
from ai_orchestrator.domain.enums import DataClassification, classification_exceeds
from ai_orchestrator.domain.errors import (
    ExternalServiceError,
    ModelRateLimited,
    ModelUnavailable,
    PolicyDenied,
    ToolUnavailable,
    ValidationError,
)

#: A 128-dim deterministic embedding. Small enough to be free to compute, large
#: enough that lexical collision is not the dominant error mode. The production
#: default is a real embedding model; see `docs/MEMORY.md` for the trade-off.
HASH_EMBEDDING_DIM = 512

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """One concrete model behind a profile."""

    provider: str
    model: str
    weight: float = 1.0
    max_input_tokens: int = 200_000
    supports_tools: bool = True
    supports_structured_output: bool = True
    supports_vision: bool = False
    pricing: ModelPricing = field(
        default_factory=lambda: ModelPricing(input_per_mtok=Money("0"), output_per_mtok=Money("0"))
    )
    # Providers that must not be used at all for a given classification.
    max_classification: DataClassification = DataClassification.RESTRICTED
    # Optional base URL for OpenAI-compatible providers.
    base_url: str | None = None
    # Optional per-candidate OpenRouter `reasoning` request object, e.g.
    # `{"enabled": False}` to disable reasoning generation on a model whose
    # catalogue entry advertises `supported_parameters: ["reasoning"]`.
    # Candidate-level only (not request-level): reasoning behaviour is a
    # property of the model, not of the caller's prompt, and a request-level
    # override would let one call silently change another candidate's
    # contract. `None` (default) sends no `reasoning` key: zero behaviour
    # change for every existing profile.
    reasoning: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_reasoning(self.reasoning)

    def key(self) -> str:
        return f"{self.provider}/{self.model}"


def _validate_reasoning(value: dict[str, Any] | None) -> None:
    """Minimal shape check for the OpenRouter `reasoning` request object."""
    if value is None:
        return
    if not isinstance(value, dict):
        msg = "model candidate reasoning must be a dict"
        raise ValidationError(msg, details={"reasoning": value})
    if "effort" in value and "max_tokens" in value:
        msg = "model candidate reasoning must not set both effort and max_tokens"
        raise ValidationError(msg, details={"reasoning": dict(value)})


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Why a particular candidate was chosen. Persisted with the usage row."""

    provider: str
    model: str
    profile: str
    reason: str
    # primary | fallback | retry | budget_downgrade
    routing_reason: str
    considered: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    #: Pre-flight estimates, recorded rather than discarded. Comparing them with
    #: the provider's reported usage is how a pricing change is noticed: a drift
    #: that is never recorded cannot be spotted.
    estimated_cost_usd: Money = field(default_factory=lambda: Money("0"))
    estimated_tokens: TokenUsage = field(default_factory=TokenUsage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
            "reason": self.reason,
            "routing_reason": self.routing_reason,
            "considered": list(self.considered),
            "rejected": list(self.rejected),
            "estimated_cost_usd": str(self.estimated_cost_usd),
            "estimated_tokens": self.estimated_tokens.total,
        }


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """A named capability requirement plus its candidate list.

    `max_classification` is a property of the *profile*, not of an individual
    candidate: it is the classification ceiling an operator set for this class
    of work. Each candidate additionally declares the ceiling its provider has
    been approved for, and the effective ceiling is the stricter of the two.
    """

    name: str
    candidates: tuple[ModelCandidate, ...]
    description: str = ""
    fallback_profile: str | None = None
    max_latency_ms: int = 60_000
    max_retries: int = 2
    requires_tools: bool = True
    requires_structured_output: bool = True
    max_classification: DataClassification = DataClassification.RESTRICTED

    def __post_init__(self) -> None:
        if not self.candidates:
            msg = f"model profile {self.name!r} has no candidates"
            raise ValidationError(msg, details={"profile": self.name})


#: Schema-object keys whose values are themselves schemas. Platform `x-`
#: facets are stripped only at these positions -- never from `properties`
#: NAMES, `enum`/`required`/`const` data, or message text.
_SUBSCHEMA_KEYS = frozenset(
    {"items", "additionalProperties", "contains", "propertyNames", "if", "then", "else", "not"}
)
_SUBSCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SUBSCHEMA_MAP_KEYS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def _strip_platform_facets(schema: Any) -> Any:
    """Copy a JSON schema without platform `x-` annotation keywords.

    Review/contract facets (`x-allow-empty`, `x-source-summary`,
    `x-human-review-order`) guide OUR validators, but strict provider grammars
    may reject unknown keywords -- so they travel in the persisted
    `expected_output_schema` and are stripped only from wire bytes. Property
    names, enum/required values and all data are preserved verbatim.
    """
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key.startswith("x-"):
                continue
            if key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
                cleaned[key] = {name: _strip_platform_facets(sub) for name, sub in value.items()}
            elif key in _SUBSCHEMA_KEYS and isinstance(value, dict):
                cleaned[key] = _strip_platform_facets(value)
            elif key in _SUBSCHEMA_LIST_KEYS and isinstance(value, list):
                cleaned[key] = [_strip_platform_facets(sub) for sub in value]
            else:
                cleaned[key] = value
        return cleaned
    if isinstance(schema, list):
        return [_strip_platform_facets(sub) for sub in schema]
    return schema


def _strip_tool_facets(tool: Any) -> Any:
    """Copy a function-tool definition with platform facets stripped from params."""
    if not isinstance(tool, dict):
        return tool
    function = tool.get("function")
    if not isinstance(function, dict) or "parameters" not in function:
        return tool
    return {
        **tool,
        "function": {**function, "parameters": _strip_platform_facets(function["parameters"])},
    }


@dataclass(slots=True)
class ModelRequest:
    """One call to the gateway."""

    profile: str
    system_instructions: str = ""
    prompt: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: str | dict[str, Any] = "auto"
    output_schema: dict[str, Any] | None = None
    max_output_tokens: int = 2_000
    temperature: float = 0.0
    data_classification: DataClassification = DataClassification.INTERNAL
    organization_id: str = ""
    # Keyed by provider, because "fallback" is a property of the attempt, not
    # of the whole request.
    excluded_providers: frozenset[str] = frozenset()
    # A bounded stage can reject a candidate whose artifact fails its contract.
    excluded_candidates: frozenset[str] = frozenset()
    estimated_input_tokens: int = 0
    remaining_budget_usd: Money | None = None
    trace_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    attempt: int = 0
    # A bounded artifact loop can reduce nested transport retries; never raises
    # the adapter's configured retry limit and is not sent to the provider.
    max_provider_retries: int | None = None
    # Evidence runtimes own validation, repair and per-attempt usage recording.
    # Ordinary agent calls use the gateway's completion checks instead.
    caller_validates_output: bool = False

    def to_provider_payload(self, candidate: ModelCandidate) -> dict[str, Any]:
        """The OpenAI-compatible chat-completions shape.

        Every provider adapter normalises to this, so adding a provider means
        writing one request builder rather than one of everything.
        """
        messages = list(self.messages)
        if self.system_instructions:
            messages = [{"role": "system", "content": self.system_instructions}, *messages]
        if self.prompt:
            messages = [*messages, {"role": "user", "content": self.prompt}]
        payload: dict[str, Any] = {
            "model": candidate.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        # temperature 0 is the default for a reason: an agent platform that
        # wants reproducible runs cannot also want sampling.
        if self.temperature > 0:
            payload["temperature"] = self.temperature
        if self.tools:
            payload["tools"] = [_strip_tool_facets(tool) for tool in self.tools]
            payload["tool_choice"] = self.tool_choice
        if self.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "schema": _strip_platform_facets(self.output_schema),
                    "strict": True,
                },
            }
        if candidate.reasoning is not None:
            # Always transmitted verbatim, including alongside tools: an
            # explicit operator value is the operator's contract with the
            # provider. No silent rewrite, no automatic per-model default
            # (a global DOTS reasoning:false default was measured to break
            # tool calls with HTTP 400, submissions 19/20).
            _validate_reasoning(candidate.reasoning)
            payload["reasoning"] = dict(candidate.reasoning)
        return payload


@dataclass(slots=True)
class ModelResponse:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    parsed: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: Money = field(default_factory=lambda: Money("0"))
    model_used: str = ""
    provider: str = ""
    latency_ms: int = 0
    decision: RoutingDecision | None = None
    finish_reason: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)
    rejected_responses: list[ModelResponse] = field(default_factory=list)
    validation_error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.usage.total


class GatewayUnavailable(ModelUnavailable):
    """Exhausted candidates, retaining returned completions for the usage ledger."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any],
        cause: BaseException | None,
        responses: tuple[ModelResponse, ...],
    ) -> None:
        super().__init__(message, details=details, cause=cause)
        self.responses = responses


class GatewayPolicyDenied(PolicyDenied):
    """Terminal provider refusal retaining known completions for the usage ledger."""

    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        super().__init__("Provider refused the request; no artifact was accepted")
        self.responses = responses


def _completion_failure(response: ModelResponse, request: ModelRequest) -> str | None:
    """Reject unusable protocol output. Business validators own semantic quality."""
    if response.finish_reason == "error":
        return "provider reported finish_reason=error"
    if response.finish_reason == "length":
        return "provider truncated the completion"
    if not response.text.strip() and not response.tool_calls:
        return "empty completion"
    requires_call = request.tool_choice == "required" or isinstance(request.tool_choice, dict)
    if request.tools and requires_call and not response.tool_calls:
        return "required tool call missing"
    allowed = {tool.get("function", {}).get("name") for tool in request.tools}
    for call in response.tool_calls:
        function = call.get("function", {})
        if function.get("name") not in allowed:
            return "unknown tool call"
        try:
            arguments = function.get("arguments", "")
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except TypeError, ValueError:
            return "malformed tool arguments"
        if not isinstance(arguments, dict):
            return "tool arguments must be an object"
    if request.output_schema is not None and not response.tool_calls:
        try:
            response.parsed = json.loads(response.text)
        except TypeError, ValueError:
            return "malformed JSON output"
    return None


class ModelProvider(abc.ABC):
    """A concrete provider adapter."""

    name: str = "abstract"

    def __init__(self, *, timeout_s: float = 60.0, max_retries: int = 2) -> None:
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    @abc.abstractmethod
    async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse: ...

    def is_configured(self) -> bool:
        """Whether a credential is present. Never returns the credential."""
        return True

    # Not `@abstractmethod`. The gateway closes every provider it holds without
    # knowing which ones hold a connection, and making this abstract would mean
    # an empty override in every stateless provider — boilerplate that looks
    # like an implementation and is not one. B027 exists to catch an author who
    # meant to make a method abstract and forgot; this one was a decision.
    async def aclose(self) -> None:  # noqa: B027
        """Release any pooled connections. Safe to call more than once."""


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_request_cost(
    request: ModelRequest, candidate: ModelCandidate
) -> tuple[TokenUsage, Money]:
    """Pre-flight cost estimate, so a call that cannot be afforded is refused
    before the provider bills for it."""
    input_text = request.system_instructions + request.prompt + json.dumps(request.messages)
    input_tokens = request.estimated_input_tokens or estimate_tokens(input_text)
    usage = TokenUsage(
        input_tokens=input_tokens,
        output_tokens=request.max_output_tokens,
    )
    return usage, candidate.pricing.cost_of(usage)


class ModelGateway:
    """Routes a request to a provider, with privacy, budget and fallback rules."""

    def __init__(
        self,
        *,
        providers: dict[str, ModelProvider] | None = None,
        profiles: dict[str, ModelProfile] | None = None,
    ) -> None:
        self._providers: dict[str, ModelProvider] = dict(providers or {})
        self._profiles: dict[str, ModelProfile] = dict(profiles or {})

    def register_provider(self, provider: ModelProvider) -> None:
        self._providers[provider.name] = provider

    def register_profile(self, profile: ModelProfile) -> None:
        self._profiles[profile.name] = profile

    @property
    def profiles(self) -> dict[str, ModelProfile]:
        return dict(self._profiles)

    def available_providers(self) -> list[str]:
        """Providers with a usable credential. Names only."""
        return sorted(name for name, p in self._providers.items() if p.is_configured())

    async def aclose(self) -> None:
        """Release provider connections owned by this gateway."""
        for provider in self._providers.values():
            await provider.aclose()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Route, call, and fall back on failure.

        Raises `ModelUnavailable` only when every candidate has failed, so the
        caller sees one terminal error rather than a partially successful chain.
        """
        profile = self._profiles.get(request.profile)
        if profile is None:
            msg = f"unknown model profile: {request.profile}"
            raise ValidationError(
                msg, details={"profile": request.profile, "known": sorted(self._profiles)}
            )

        candidates, rejected = self._select_candidates(profile, request)
        if not candidates:
            msg = (
                f"no permitted model candidate for profile {request.profile!r} "
                f"(classification={request.data_classification.value}); "
                f"rejected: {sorted(rejected)}"
            )
            raise ModelUnavailable(msg, details={"profile": request.profile, "rejected": rejected})

        considered: list[str] = []
        errors: list[str] = []
        #: Candidates that were never *servable* -- no adapter, or one that is not
        #: configured. Kept apart from the failures below because "we tried it and
        #: it failed" and "it was never wired up" are different facts, and a
        #: single message reading "every model candidate failed" makes an
        #: impossible candidate look like a flaky one. That is not a cosmetic
        #: difference: the first means retry, the second means fix configuration.
        unservable: list[str] = []
        primary_error: BaseException | None = None
        rejected_responses: list[ModelResponse] = []
        spent = Money("0")

        for index, candidate in enumerate(candidates):
            considered.append(candidate.key())
            if candidate.key() in request.excluded_candidates:
                errors.append(candidate.key() + ": excluded after artifact verification failure")
                continue
            provider = self._providers.get(candidate.provider)
            if provider is None:
                unservable.append(f"{candidate.key()}: no provider adapter registered")
                continue
            if not provider.is_configured():
                unservable.append(f"{candidate.key()}: provider not configured")
                continue

            usage_estimate, cost_estimate = estimate_request_cost(request, candidate)
            if (
                request.remaining_budget_usd is not None
                and cost_estimate > request.remaining_budget_usd - spent
            ):
                # Refusing is better than calling and discovering afterwards that
                # the org is over budget.
                errors.append(
                    f"{candidate.key()}: estimated {cost_estimate} USD exceeds the remaining budget"
                )
                continue

            decision = RoutingDecision(
                provider=candidate.provider,
                model=candidate.model,
                profile=profile.name,
                reason=(
                    "primary candidate"
                    if index == 0
                    else f"fallback after {len(errors)} failure(s)"
                ),
                routing_reason="primary" if index == 0 else "fallback",
                considered=tuple(considered),
                rejected=tuple(sorted(rejected)) + tuple(errors),
                estimated_cost_usd=cost_estimate,
                estimated_tokens=usage_estimate,
            )

            try:
                retries = max(0, profile.max_retries)
                if request.max_provider_retries is not None:
                    retries = min(retries, max(0, request.max_provider_retries))
                bounded = replace(request, max_provider_retries=retries)
                candidate_started = time.monotonic()
                async with asyncio.timeout(profile.max_latency_ms / 1000):
                    response = await provider.complete(candidate, bounded)
            except asyncio.CancelledError:
                # Cancellation is not a candidate failure: it must propagate
                # without being recorded as a fallback or swallowed into
                # GatewayUnavailable. Cost stays unknown, never zero-filled.
                elapsed_ms = int((time.monotonic() - candidate_started) * 1000)
                logger.warning(
                    "model candidate cancelled: %s after %sms",
                    candidate.key(),
                    elapsed_ms,
                )
                raise
            except TimeoutError as exc:
                errors.append(f"{candidate.key()}: candidate deadline exceeded")
                primary_error = primary_error or exc
                continue
            except ModelRateLimited as exc:
                errors.append(f"{candidate.key()}: rate limited")
                primary_error = primary_error or exc
                continue
            except (ModelUnavailable, ExternalServiceError, ToolUnavailable) as exc:
                errors.append(f"{candidate.key()}: {exc.message}")
                primary_error = primary_error or exc
                continue

            response.decision = decision
            response.model_used = response.model_used or candidate.model
            response.provider = candidate.provider
            if response.finish_reason in {"refused", "content_filter"}:
                response.validation_error = "Provider refused the request"
                raise GatewayPolicyDenied((*rejected_responses, response))
            if not request.caller_validates_output:
                finding = _completion_failure(response, request)
                if finding:
                    response.validation_error = finding
                    rejected_responses.append(response)
                    spent += response.cost_usd
                    errors.append(f"{candidate.key()}: {finding}")
                    continue
            response.rejected_responses = rejected_responses
            return response

        # Both lists, and which is which. On a real free-tier run this read
        # "every model candidate failed: ... rate limited; ... no provider adapter
        # registered" -- which invites a retry for a candidate that was never
        # reachable, and hides the fact that the profile promised a fallback the
        # gateway cannot serve.
        detail = "; ".join(errors + unservable) or "no candidates attempted"
        raise GatewayUnavailable(
            f"no usable model for profile {request.profile!r}: {detail}",
            details={
                "profile": request.profile,
                "errors": errors,
                "unservable": unservable,
                "considered": considered,
            },
            cause=primary_error,
            responses=tuple(rejected_responses),
        )

    def _select_candidates(
        self, profile: ModelProfile, request: ModelRequest
    ) -> tuple[list[ModelCandidate], set[str]]:
        """Apply the filters, most restrictive first."""
        rejected: set[str] = set()
        candidates: list[ModelCandidate] = []

        for candidate in profile.candidates:
            key = candidate.key()
            if candidate.provider in request.excluded_providers:
                rejected.add(f"{key}: excluded by an earlier failure")
                continue
            # Privacy first. A restricted document must never reach a provider
            # the policy has not approved, whatever it costs or how fast it is.
            # The effective ceiling is the stricter of the profile's and the
            # provider's, so neither can be widened by editing the other.
            effective_ceiling = _most_restrictive(
                candidate.max_classification, profile.max_classification
            )
            if classification_exceeds(request.data_classification, effective_ceiling):
                rejected.add(
                    f"{key}: classification {request.data_classification.value} exceeds "
                    f"the effective ceiling {effective_ceiling.value}"
                )
                continue
            if profile.requires_tools and not candidate.supports_tools:
                rejected.add(f"{key}: profile requires tool calling")
                continue
            if (
                profile.requires_structured_output or request.output_schema is not None
            ) and not candidate.supports_structured_output:
                rejected.add(f"{key}: profile requires structured output")
                continue
            if request.estimated_input_tokens > candidate.max_input_tokens:
                rejected.add(
                    f"{key}: input of {request.estimated_input_tokens} tokens exceeds "
                    f"the model's {candidate.max_input_tokens}"
                )
                continue
            candidates.append(candidate)

        # Preserve the profile's declared order. It is the operator's stated
        # preference, and re-sorting by weight here would make the profile's
        # ordering a suggestion.
        return candidates, rejected


def _most_restrictive(*ceilings: DataClassification) -> DataClassification:
    """The strictest of several classification ceilings.

    A ceiling is an upper bound on what may be sent. When a profile and a
    provider both declare one, the stricter wins: neither can widen the other by
    being edited alone.
    """
    order = (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
        DataClassification.CONFIDENTIAL,
        DataClassification.RESTRICTED,
        DataClassification.SECRET,
    )
    return min(ceilings, key=order.index)


def hash_embedding(text: str, dimensions: int = HASH_EMBEDDING_DIM) -> list[float]:
    """A deterministic, offline embedding.

    Bag-of-tokens hashed into a fixed number of dimensions and L2-normalised. It
    is not semantic — two sentences about different topics with the same words
    score highly, and paraphrases score poorly. What it *does* give is
    reproducible, zero-cost, network-free retrieval for tests, and a stable
    baseline to measure a real embedding model against.

    Chosen as the test/CI default precisely so a test suite cannot silently
    depend on a paid provider.
    """
    vector = [0.0] * dimensions
    tokens = _tokenize(text)
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        # The 5th byte carries the sign, so distinct tokens do not all
        # reinforce in the same direction and the mean stays near zero.
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = sum(v * v for v in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def _tokenize(text: str) -> list[str]:
    import re

    return [t for t in re.findall(r"[\w']+", text.lower()) if len(t) > 1]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    value = float(dot / (na * nb))
    return max(-1.0, min(1.0, value))


def price_from_per_mtok(input_usd: str, output_usd: str) -> ModelPricing:
    return ModelPricing(
        input_per_mtok=Decimal(input_usd),
        output_per_mtok=Decimal(output_usd),
    )


__all__ = [
    "HASH_EMBEDDING_DIM",
    "ModelCandidate",
    "ModelGateway",
    "ModelPricing",
    "ModelProfile",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "RoutingDecision",
    "cosine_similarity",
    "estimate_request_cost",
    "hash_embedding",
    "price_from_per_mtok",
]
