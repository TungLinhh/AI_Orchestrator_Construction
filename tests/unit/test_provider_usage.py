"""OpenRouter completion usage includes reasoning, while our buckets add up."""

import pytest

from ai_orchestrator.domain.budget import ModelPricing, Money
from ai_orchestrator.domain.errors import ModelUnavailable
from ai_orchestrator.models.gateway import ModelCandidate
from ai_orchestrator.models.providers import _parse_openai_response


@pytest.mark.parametrize("reasoning", [0, 40, 50])
def test_completion_breakdown_matches_provider_total_and_preserves_cost(reasoning):
    raw_usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "completion_tokens_details": {"reasoning_tokens": reasoning},
        "cost": "0.0002",
    }
    response = _parse_openai_response(
        {"choices": [{"message": {"content": "Fixture"}}], "usage": raw_usage},
        ModelCandidate(provider="openrouter", model="fixture"),
        1,
    )
    assert response.usage.total == raw_usage["total_tokens"]
    assert response.usage.output_tokens == 50 - reasoning
    assert response.usage.reasoning_tokens == reasoning
    assert response.raw_usage == raw_usage
    assert response.cost_usd == Money("0.0002")


def test_missing_cost_estimates_full_completion_once():
    response = _parse_openai_response(
        {
            "choices": [{"message": {"content": "Fixture"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 40},
            },
        },
        ModelCandidate(
            provider="openrouter",
            model="fixture",
            pricing=ModelPricing(input_per_mtok=Money("1"), output_per_mtok=Money("2")),
        ),
        1,
    )
    assert response.cost_usd == Money("0.0002")


@pytest.mark.parametrize("reasoning", [-1, 51])
def test_inconsistent_usage_is_refused_instead_of_silently_clamped(reasoning):
    with pytest.raises(ModelUnavailable, match="inconsistent completion/reasoning"):
        _parse_openai_response(
            {
                "choices": [{"message": {"content": "Fixture"}}],
                "usage": {
                    "completion_tokens": 50,
                    "completion_tokens_details": {"reasoning_tokens": reasoning},
                },
            },
            ModelCandidate(provider="openrouter", model="fixture"),
            1,
        )
