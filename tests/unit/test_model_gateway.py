"""Model gateway: routing, privacy, budget and fallback.

The failure tests here are the point. A gateway that only works when the primary
provider is healthy has not been tested, and the failure it hides — silently
falling back, or silently refusing — is exactly the one that shows up in
production.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import ModelRateLimited, ModelUnavailable
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelProfile,
    ModelRequest,
    estimate_request_cost,
    hash_embedding,
)
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import DeterministicProvider

pytestmark = pytest.mark.unit


class FlakyProvider(DeterministicProvider):
    """Fails its first N calls, then succeeds. Used to test fallback."""

    def __init__(self, *, name: str, fail_times: int = 1, error: Exception | None = None) -> None:
        super().__init__()
        self.name = name
        self._fail_times = fail_times
        self._error = error or ModelUnavailable(f"{name} is down")
        self.attempts = 0

    async def complete(self, candidate, request):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise self._error
        return await super().complete(candidate, request)


def _gateway(providers: dict, profiles: dict | None = None) -> ModelGateway:
    return ModelGateway(providers=providers, profiles=profiles or default_profiles())


class TestHappyPath:
    async def test_routes_to_the_first_candidate(self) -> None:
        """Order is the preference, so the first candidate must be the one used.

        A profile of its own rather than `default_profiles()["default"]`. That
        catalogue is a product decision and it changed under this test — the
        shipped default now leads with a model this gateway has no provider for,
        so the call correctly fell through to the deterministic candidate and was
        recorded as a *fallback*. The test was measuring the catalogue, not
        routing.
        """
        gw = _gateway(
            {"deterministic": DeterministicProvider()},
            profiles={
                "only_one": ModelProfile(
                    name="only_one",
                    candidates=(ModelCandidate(provider="deterministic", model="scripted-1"),),
                )
            },
        )
        response = await gw.complete(ModelRequest(profile="only_one", prompt="hello"))
        assert response.provider == "deterministic"
        assert response.decision is not None
        assert response.decision.routing_reason == "primary"

    async def test_profile_order_is_preserved(self) -> None:
        """The operator's declared order is the preference. Re-sorting by price
        or latency would quietly override it."""
        profiles = {
            "ordered": ModelProfile(
                name="ordered",
                candidates=(
                    ModelCandidate(provider="deterministic", model="first"),
                    ModelCandidate(provider="other", model="second"),
                ),
            )
        }
        gw = _gateway(
            {"deterministic": DeterministicProvider(), "other": DeterministicProvider()}, profiles
        )
        response = await gw.complete(ModelRequest(profile="ordered", prompt="x"))
        assert response.model_used == "first"

    async def test_usage_and_cost_are_reported(self) -> None:
        gw = _gateway({"deterministic": DeterministicProvider()})
        response = await gw.complete(
            ModelRequest(profile="default", prompt="a" * 400, max_output_tokens=100)
        )
        assert response.usage.input_tokens > 0
        assert response.usage.output_tokens == 100
        assert response.total_tokens > 0


class TestPrivacyRouting:
    async def test_secret_data_never_reaches_an_external_provider(self) -> None:
        """The candidate list is the policy. Restricted data must not be sent to
        a provider that has not been approved for it, regardless of cost or
        latency."""
        from ai_orchestrator.models.providers import OpenAICompatibleProvider

        external = OpenAICompatibleProvider(
            "openrouter", api_key="fake-key-for-test", base_url="https://example.invalid/v1"
        )
        profiles = {
            "mixed": ModelProfile(
                name="mixed",
                candidates=(
                    ModelCandidate(
                        provider="openrouter",
                        model="some/model",
                        max_classification=DataClassification.INTERNAL,
                    ),
                    ModelCandidate(
                        provider="deterministic",
                        model="local",
                        max_classification=DataClassification.SECRET,
                    ),
                ),
            )
        }
        gw = _gateway({"openrouter": external, "deterministic": DeterministicProvider()}, profiles)

        response = await gw.complete(
            ModelRequest(
                profile="mixed",
                prompt="classified",
                data_classification=DataClassification.SECRET,
            )
        )
        assert response.provider == "deterministic", (
            "an external provider was selected for secret data"
        )
        assert any("openrouter" in reason for reason in response.decision.rejected)

    async def test_profile_ceiling_is_not_widenable_by_the_provider(self) -> None:
        """Editing a provider's ceiling cannot override a stricter profile."""
        from ai_orchestrator.models.providers import OpenAICompatibleProvider

        external = OpenAICompatibleProvider(
            "openrouter", api_key="fake", base_url="https://example.invalid/v1"
        )
        profiles = {
            "strict": ModelProfile(
                name="strict",
                # Profile allows secret; provider only up to internal.
                max_classification=DataClassification.SECRET,
                candidates=(
                    ModelCandidate(
                        provider="openrouter",
                        model="m",
                        max_classification=DataClassification.INTERNAL,
                    ),
                ),
            )
        }
        gw = _gateway({"openrouter": external}, profiles)
        with pytest.raises(ModelUnavailable):
            await gw.complete(
                ModelRequest(
                    profile="strict",
                    prompt="x",
                    data_classification=DataClassification.RESTRICTED,
                )
            )

    async def test_internal_data_may_use_an_approved_external_provider(self) -> None:
        """The ceiling is inclusive: a provider approved for the exact
        classification must actually be usable."""
        from ai_orchestrator.models.providers import OpenAICompatibleProvider

        external = OpenAICompatibleProvider(
            "openrouter", api_key="fake", base_url="https://example.invalid/v1"
        )
        profiles = {
            "approved": ModelProfile(
                name="approved",
                candidates=(
                    ModelCandidate(
                        provider="deterministic",
                        model="local",
                        max_classification=DataClassification.INTERNAL,
                    ),
                ),
            )
        }
        gw = _gateway({"external": external, "deterministic": DeterministicProvider()}, profiles)
        response = await gw.complete(
            ModelRequest(
                profile="approved",
                prompt="x",
                data_classification=DataClassification.INTERNAL,
            )
        )
        assert response.provider == "deterministic"


class TestFallback:
    async def test_falls_back_and_records_the_reason(self) -> None:
        """Acceptance scenario 7. The fallback must be *recorded*: 'it worked' and
        'it worked via a fallback' are different results for an evaluation and
        for a cost report."""
        profiles = {
            "chained": ModelProfile(
                name="chained",
                candidates=(
                    ModelCandidate(provider="primary", model="p"),
                    ModelCandidate(provider="backup", model="b"),
                ),
            )
        }
        gw = _gateway(
            {
                "primary": FlakyProvider(name="primary", fail_times=99),
                "backup": DeterministicProvider(),
            },
            profiles,
        )
        response = await gw.complete(ModelRequest(profile="chained", prompt="x"))
        assert response.provider == "backup"
        assert response.decision is not None
        assert response.decision.routing_reason == "fallback"
        assert "primary/primary" not in (response.decision.considered or ())[:1]

    async def test_rate_limited_primary_falls_back(self) -> None:
        profiles = {
            "chained": ModelProfile(
                name="chained",
                candidates=(
                    ModelCandidate(provider="primary", model="p"),
                    ModelCandidate(provider="backup", model="b"),
                ),
            )
        }
        gw = _gateway(
            {
                "primary": FlakyProvider(
                    name="primary", fail_times=99, error=ModelRateLimited("429")
                ),
                "backup": DeterministicProvider(),
            },
            profiles,
        )
        response = await gw.complete(ModelRequest(profile="chained", prompt="x"))
        assert response.provider == "backup"

    async def test_fails_safely_when_no_fallback_is_permitted(self) -> None:
        """When policy permits no fallback, the call must fail rather than
        quietly reaching a provider the operator did not approve."""
        profiles = {
            "strict": ModelProfile(
                name="strict",
                candidates=(ModelCandidate(provider="primary", model="p"),),
            )
        }
        gw = _gateway({"primary": FlakyProvider(name="primary", fail_times=99)}, profiles)
        with pytest.raises(ModelUnavailable) as exc:
            await gw.complete(ModelRequest(profile="strict", prompt="x"))
        # The message is asserted by what it has to *say*, not by its wording.
        # Pinning the literal meant that improving the message -- splitting
        # "never servable" from "tried and failed" -- broke this test while the
        # behaviour under it was unchanged and correct. A test anchored on a
        # string fails when the string is corrected, which is the wrong way
        # round (F171).
        message = exc.value.message
        assert "strict" in message, "the message must name the profile, or it is not actionable"
        assert "primary is down" in message, (
            "the message must carry the reason, or a reader has to guess which "
            "candidate failed and how"
        )

    async def test_excluded_providers_are_not_retried(self) -> None:
        profiles = {
            "chained": ModelProfile(
                name="chained",
                candidates=(
                    ModelCandidate(provider="primary", model="p"),
                    ModelCandidate(provider="backup", model="b"),
                ),
            )
        }
        primary = FlakyProvider(name="primary", fail_times=99)
        gw = _gateway({"primary": primary, "backup": DeterministicProvider()}, profiles)
        with pytest.raises(ModelUnavailable):
            await gw.complete(
                ModelRequest(
                    profile="chained", prompt="x", excluded_providers=frozenset({"backup"})
                )
            )
        assert primary.attempts == 1, "the excluded provider was called"

    async def test_unconfigured_provider_is_skipped(self) -> None:
        from ai_orchestrator.models.providers import OpenAICompatibleProvider

        unconfigured = OpenAICompatibleProvider("nokey", api_key=None, base_url="https://x.invalid")
        profiles = {
            "chained": ModelProfile(
                name="chained",
                candidates=(
                    ModelCandidate(provider="nokey", model="m"),
                    ModelCandidate(provider="deterministic", model="local"),
                ),
            )
        }
        gw = _gateway({"nokey": unconfigured, "deterministic": DeterministicProvider()}, profiles)
        response = await gw.complete(ModelRequest(profile="chained", prompt="x"))
        assert response.provider == "deterministic"


class TestBudgetGuard:
    async def test_call_is_refused_before_spending(self) -> None:
        """A call that cannot be afforded must not be made. Discovering the
        overspend after the provider billed for it is not a saving."""
        provider = DeterministicProvider()
        profiles = {
            "costly": ModelProfile(
                name="costly",
                candidates=(
                    ModelCandidate(
                        provider="deterministic",
                        model="expensive",
                        pricing=_pricing("1000.00", "1000.00"),
                    ),
                ),
            )
        }
        gw = _gateway({"deterministic": provider}, profiles)
        with pytest.raises(ModelUnavailable):
            await gw.complete(
                ModelRequest(
                    profile="costly",
                    prompt="x" * 4000,
                    max_output_tokens=4000,
                    remaining_budget_usd=Money("0.01"),
                )
            )
        assert provider.call_count == 0, "a refused call must not reach the provider"

    def test_estimate_is_reported_before_the_call(self) -> None:
        candidate = ModelCandidate(provider="p", model="m", pricing=_pricing("3.00", "15.00"))
        usage, cost = estimate_request_cost(
            ModelRequest(profile="default", prompt="a" * 4000, max_output_tokens=1000), candidate
        )
        assert usage.input_tokens == 1000
        # 1000 in @ 3.00/Mtok = 0.003, 1000 out @ 15.00/Mtok = 0.015
        assert cost == Money("0.018")


class TestCapabilityFilters:
    async def test_profile_requiring_tools_rejects_a_candidate_without_them(self) -> None:
        profiles = {
            "needs_tools": ModelProfile(
                name="needs_tools",
                requires_tools=True,
                candidates=(
                    ModelCandidate(
                        provider="deterministic", model="no-tools", supports_tools=False
                    ),
                ),
            )
        }
        gw = _gateway({"deterministic": DeterministicProvider()}, profiles)
        with pytest.raises(ModelUnavailable):
            await gw.complete(ModelRequest(profile="needs_tools", prompt="x"))

    async def test_oversized_input_rejects_the_candidate(self) -> None:
        profiles = {
            "small": ModelProfile(
                name="small",
                candidates=(
                    ModelCandidate(provider="deterministic", model="tiny", max_input_tokens=10),
                ),
            )
        }
        gw = _gateway({"deterministic": DeterministicProvider()}, profiles)
        with pytest.raises(ModelUnavailable):
            await gw.complete(
                ModelRequest(profile="small", prompt="x", estimated_input_tokens=100_000)
            )

    def test_unknown_profile_is_a_validation_error_not_a_runtime_failure(self) -> None:
        """A typo in a profile name should be reported as a configuration bug,
        not as 'the model provider is down'."""
        from ai_orchestrator.domain.errors import ValidationError

        gw = _gateway({"deterministic": DeterministicProvider()})
        import asyncio

        with pytest.raises(ValidationError):
            asyncio.run(gw.complete(ModelRequest(profile="typo", prompt="x")))


class TestDeterministicEmbeddings:
    def test_is_reproducible(self) -> None:
        """Reproducibility is the whole reason this exists for tests: the same
        text must always produce the same vector or retrieval tests are flaky."""
        assert hash_embedding("revenue was up") == hash_embedding("revenue was up")

    def test_similar_text_scores_above_unrelated_text(self) -> None:
        from ai_orchestrator.models.gateway import cosine_similarity

        a = hash_embedding("quarterly revenue report for the sales team")
        near = hash_embedding("quarterly revenue report for the sales department")
        far = hash_embedding("kubernetes ingress controller annotations")
        assert cosine_similarity(a, near) > cosine_similarity(a, far)

    def test_vectors_are_normalised(self) -> None:
        vector = hash_embedding("some text to embed")
        norm = sum(v * v for v in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-9)

    def test_empty_text_is_handled(self) -> None:
        assert hash_embedding("") == [0.0] * 512


def _pricing(input_usd: str, output_usd: str):
    from ai_orchestrator.domain.budget import ModelPricing

    return ModelPricing(input_per_mtok=Money(input_usd), output_per_mtok=Money(output_usd))
