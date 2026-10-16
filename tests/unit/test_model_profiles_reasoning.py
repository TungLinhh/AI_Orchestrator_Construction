"""`reasoning` at the model-profile config boundary (offline, no DB).

`ModelCandidate` accepts an optional `reasoning` dict and `to_provider_payload`
transmits it verbatim. This is an explicit-operator capability only: there is
no automatic per-model default (a global DOTS reasoning:false default was
measured to break tool calls with HTTP 400, submissions 19/20), so rows
without the key send nothing and normal tool tasks use provider-default
reasoning. These tests pin that boundary:

* a row entry with `reasoning` loads and the payload carries it verbatim,
  including alongside tools (no silent rewrite);
* without the key, every model stays `None` and the payload omits the key;
* malformed `reasoning` is rejected with the gateway's validation semantics;
* builtin candidates carry no `reasoning`;
* explicit override and explicit null are preserved as-is.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_orchestrator.application.model_profiles import _candidate
from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.models.gateway import ModelRequest
from ai_orchestrator.models.profiles import default_profiles

NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free"
DOTS = "dots-studio/dots-3-note-preview:free"


def _load(entry: dict[str, Any]) -> tuple[Any, list[str]]:
    warnings: list[str] = []
    candidate = _candidate(entry, where="test", warn=warnings.append)
    assert candidate is not None
    return candidate, warnings


def test_reasoning_enabled_false_loads_and_reaches_payload() -> None:
    candidate, _ = _load(
        {
            "model": DOTS,
            "provider": "openrouter",
            "reasoning": {"enabled": False},
        }
    )
    assert candidate.reasoning == {"enabled": False}
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert payload["reasoning"] == {"enabled": False}


def test_explicit_reasoning_transmitted_verbatim_alongside_tools() -> None:
    # No silent rewrite: an explicit operator value is the operator's
    # contract with the provider, even when tools are present.
    candidate, _ = _load(
        {
            "model": DOTS,
            "provider": "openrouter",
            "reasoning": {"enabled": False},
        }
    )
    payload = ModelRequest(
        profile="p", prompt="hi", tools=[{"type": "function", "function": {"name": "t"}}]
    ).to_provider_payload(candidate)
    assert payload["reasoning"] == {"enabled": False}


def test_missing_reasoning_key_means_none_and_no_payload_key() -> None:
    candidate, _ = _load({"model": "m", "provider": "openrouter"})
    assert candidate.reasoning is None
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert "reasoning" not in payload


def test_effort_and_max_tokens_together_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            {
                "model": "m",
                "provider": "openrouter",
                "reasoning": {"effort": "high", "max_tokens": 512},
            }
        )


def test_non_dict_reasoning_rejected() -> None:
    with pytest.raises(ValidationError):
        _load({"model": "m", "provider": "openrouter", "reasoning": "off"})


def test_reasoning_round_trip_preserves_dict_verbatim() -> None:
    original = {"effort": "high", "exclude": True}
    candidate, _ = _load({"model": "m", "provider": "openrouter", "reasoning": original})
    assert candidate.reasoning == original
    assert candidate.reasoning is not original
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert payload["reasoning"] == original


def test_unknown_keys_still_warned_and_ignored() -> None:
    candidate, warnings = _load(
        {
            "model": "m",
            "provider": "openrouter",
            "reasoning": {"enabled": False},
            "bogus_key": 1,
        }
    )
    assert candidate.reasoning == {"enabled": False}
    assert any("bogus_key" in warning for warning in warnings)


def test_builtin_dots_candidate_carries_no_reasoning() -> None:
    # No automatic default: the builtin DOTS fallback sends no reasoning key,
    # so tool tasks use provider-default reasoning (submissions 19/20).
    profiles = default_profiles()
    dots = next(
        c for c in profiles["primary"].candidates if c.model == DOTS and c.provider == "openrouter"
    )
    assert dots.reasoning is None
    payload = ModelRequest(profile="primary", prompt="hi").to_provider_payload(dots)
    assert "reasoning" not in payload


def test_builtin_nemotron_candidate_has_no_reasoning() -> None:
    profiles = default_profiles()
    nemotron = next(
        c
        for c in profiles["primary"].candidates
        if c.model == NEMOTRON and c.provider == "openrouter"
    )
    assert nemotron.reasoning is None
    payload = ModelRequest(profile="primary", prompt="hi").to_provider_payload(nemotron)
    assert "reasoning" not in payload


def _legacy_primary_entries() -> list[dict[str, Any]]:
    """Legacy tenant-style JSON profile: primary with nemotron+dots, NO reasoning keys."""
    return [
        {"model": NEMOTRON, "provider": "openrouter", "weight": 1.0},
        {"model": DOTS, "provider": "openrouter", "weight": 1.0},
    ]


def test_legacy_profile_without_reasoning_keys_sends_none() -> None:
    warnings: list[str] = []
    loaded = [
        _candidate(e, where="test primary", warn=warnings.append) for e in _legacy_primary_entries()
    ]
    assert all(c is not None for c in loaded)
    by_model = {c.model: c for c in loaded}  # type: ignore[union-attr]
    for model in (NEMOTRON, DOTS):
        assert by_model[model].reasoning is None
        payload = ModelRequest(profile="primary", prompt="hi").to_provider_payload(by_model[model])
        assert "reasoning" not in payload


def test_explicit_operator_override_preserved_verbatim() -> None:
    candidate, _ = _load({"model": DOTS, "provider": "openrouter", "reasoning": {"effort": "low"}})
    assert candidate.reasoning == {"effort": "low"}
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert payload["reasoning"] == {"effort": "low"}


def test_explicit_null_stays_none_and_omits_payload_key() -> None:
    candidate, _ = _load({"model": DOTS, "provider": "openrouter", "reasoning": None})
    assert candidate.reasoning is None
    payload = ModelRequest(profile="p", prompt="hi").to_provider_payload(candidate)
    assert "reasoning" not in payload
