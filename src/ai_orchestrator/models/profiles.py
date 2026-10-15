"""Model profile catalogue.

The mapping from a *capability requirement* to concrete models. This is the file
an operator edits to change provider, and it is deliberately data rather than
code: swapping a model behind a profile must not require a deploy, and no agent
definition should ever name a provider.

Three principles in the defaults:

  * a profile's candidates are in preference order, and the gateway preserves
    that order. Re-sorting by price or latency would quietly override the
    operator's stated preference;
  * a profile has at least one candidate that is not the most capable one, so a
    budget-constrained run can degrade in quality instead of failing;
  * `max_classification` is set per candidate. A provider that has not been
    approved for restricted data simply does not appear for it.
"""

from __future__ import annotations

from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.models.gateway import ModelCandidate, ModelPricing, ModelProfile
from ai_orchestrator.models.providers import DeterministicProvider


def _pricing(input_usd: str, output_usd: str) -> ModelPricing:
    return ModelPricing(
        input_per_mtok=Money(input_usd),
        output_per_mtok=Money(output_usd),
    )


#: Available only when explicit fake mode registers the deterministic adapter.
_DETERMINISTIC = ModelCandidate(
    provider="deterministic",
    model="scripted-1",
    pricing=_pricing("0", "0"),
    max_classification=DataClassification.SECRET,
)

#: Explicit execution chain requested by the operator. Catalogue checked 2026-10-15.
_PRIMARY_FREE = ModelCandidate(
    provider="openrouter",
    model="nvidia/nemotron-3-ultra-550b-a55b:free",
    max_input_tokens=1_000_000,
    supports_structured_output=False,
    pricing=_pricing("0", "0"),
    max_classification=DataClassification.PUBLIC,
)
_FREE_FALLBACKS: tuple[ModelCandidate, ...] = (
    ModelCandidate(
        provider="openrouter",
        model="dots-studio/dots-3-note-preview:free",
        max_input_tokens=512_000,
        supports_vision=True,
        pricing=_pricing("0", "0"),
        max_classification=DataClassification.PUBLIC,
    ),
)


def default_profiles() -> dict[str, ModelProfile]:
    """The built-in profile catalogue.

    Prices are the published list prices at the time of writing and are
    documentation, not billing truth: when a provider reports its own cost, that
    number wins. See `_cost_from_body` in `providers.py`.
    """
    return {
        # NVIDIA supports tools but does not advertise strict JSON Schema. Workflows
        # validate their tool output independently.
        # Live availability still depends on provider capacity and quota.
        #
        # Placed ahead of the deterministic provider on purpose: the point of
        # this deployment is a real model deciding real work. A profile whose
        # first candidate is the scripted stub would never ask it anything.
        "primary": ModelProfile(
            name="primary",
            requires_structured_output=False,
            max_latency_ms=60_000,
            max_retries=0,
            description="The operator's model. Real reasoning, no cost.",
            candidates=(
                _PRIMARY_FREE,
                *_FREE_FALLBACKS,
                _DETERMINISTIC,
            ),
        ),
        "reasoning": ModelProfile(
            name="reasoning",
            requires_structured_output=False,
            max_latency_ms=60_000,
            max_retries=0,
            description=(
                "Analysis and planning. The same model with tools, because the "
                "operator asked for one model rather than a portfolio."
            ),
            candidates=(
                _PRIMARY_FREE,
                *_FREE_FALLBACKS,
                _DETERMINISTIC,
            ),
        ),
        "proposer": ModelProfile(
            name="proposer",
            description=(
                "Writes the lesson proposal from a finished run. **A different "
                "model from the one that did the work**, and never a script: a "
                "proposal nobody wrote would be put in front of a human as though "
                "a model had written it, and the platform refuses a subject that "
                "judges itself."
            ),
            candidates=(
                # **Disjoint from `primary` by construction.** The first attempt
                # at this reused two of the free fallbacks, and the sets then
                # overlapped -- the subject judging itself, which is the exact
                # failure the profile exists to prevent. So the candidates are
                # named from the free list *after* `primary`'s, and
                # `test_the_proposer_is_not_the_operator_model` is the instrument
                # that keeps them apart when either list changes.
                ModelCandidate(
                    provider="openrouter",
                    model="google/gemma-4-31b-it:free",
                    pricing=_pricing("0", "0"),
                    max_classification=DataClassification.PUBLIC,
                ),
                ModelCandidate(
                    provider="openrouter",
                    model="google/gemma-4-26b-a4b-it:free",
                    max_input_tokens=262144,
                    pricing=_pricing("0", "0"),
                    max_classification=DataClassification.PUBLIC,
                ),
            ),
            # The whole interface is one tool call, so a proposer that cannot call
            # tools returns prose and the platform reads it as "no lesson" --
            # indistinguishable from a model that looked and found nothing.
            requires_tools=True,
            # No fallback profile: falling back to `default` reintroduces the
            # scripted candidate through the back door.
            fallback_profile=None,
        ),
        "default": ModelProfile(
            name="default",
            requires_structured_output=False,
            max_latency_ms=60_000,
            max_retries=0,
            description=(
                "General purpose. The operator's free models first, because a "
                "profile whose first candidate is a paid one this key cannot "
                "reach just burns a 403 before falling back."
            ),
            candidates=(
                _PRIMARY_FREE,
                *_FREE_FALLBACKS,
                ModelCandidate(
                    provider="openrouter",
                    model="openai/gpt-4.1-mini",
                    pricing=_pricing("0.40", "1.60"),
                    max_classification=DataClassification.RESTRICTED,
                ),
                _DETERMINISTIC,
            ),
        ),
    }


__all__ = ["DeterministicProvider", "default_profiles"]
