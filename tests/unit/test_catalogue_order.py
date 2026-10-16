"""Builtin catalogue order: DOTS-first, NVIDIA-fallback.

Scoped tenant reorders proved DOTS-first on the live chain (round-4: 7
persisted department tasks completed with schema/source/grade PASS), while the
NVIDIA primary cost the chain-acceptance root its acknowledgement turn. Fresh
orgs inherit this builtin order, so it starts DOTS-first instead of repeating
the bad primary. The reorder moves entries only: every capability, privacy and
pricing field below is pinned, so a future edit cannot smuggle a field change
inside a reorder. An explicitly persisted operator order still wins through
the loader overlay, which replaces by name and is tested here without a
database by constructing profile rows directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_orchestrator.application.model_profiles import load_profiles_from_session
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.models.profiles import default_profiles

pytestmark = pytest.mark.unit

DOTS = "dots-studio/dots-3-note-preview:free"
NVIDIA = "nvidia/nemotron-3-ultra-550b-a55b:free"


class TestBuiltinPrimaryIsDotsFirst:
    def test_primary_and_reasoning_lead_with_dots(self) -> None:
        profiles = default_profiles()
        for name in ("primary", "reasoning"):
            models = [c.model for c in profiles[name].candidates]
            assert models[0] == DOTS, f"{name} leads with {models[0]}"
            assert models[1] == NVIDIA, f"{name} falls back to {models[1]}"
            assert models[-1] == "scripted-1", f"{name} keeps the scripted tail"

    def test_per_model_fields_are_byte_identical_to_the_nvidia_first_revision(
        self,
    ) -> None:
        """The reorder moves entries only. Each candidate keeps its exact
        capability/privacy/pricing fields; only the tuple position changed."""
        by_model = {}
        for profile in ("primary", "reasoning"):
            for candidate in default_profiles()[profile].candidates:
                by_model.setdefault(candidate.model, candidate)
        dots = by_model[DOTS]
        assert dots.provider == "openrouter"
        assert dots.max_input_tokens == 512_000
        assert dots.supports_vision is True
        assert str(dots.pricing.input_per_mtok) == "0"
        assert str(dots.pricing.output_per_mtok) == "0"
        assert dots.max_classification is DataClassification.PUBLIC
        nvidia = by_model[NVIDIA]
        assert nvidia.provider == "openrouter"
        assert nvidia.max_input_tokens == 1_000_000
        assert nvidia.supports_structured_output is False
        assert str(nvidia.pricing.input_per_mtok) == "0"
        assert str(nvidia.pricing.output_per_mtok) == "0"
        assert nvidia.max_classification is DataClassification.PUBLIC

    def test_the_default_profile_keeps_its_existing_order(self) -> None:
        """Out of scope for the reorder: `default` is a different profile and
        its effective order is unchanged, so this test fails if the shared
        constants ever drag it along silently."""
        models = [c.model for c in default_profiles()["default"].candidates]
        assert models == [
            NVIDIA,
            DOTS,
            "openai/gpt-4.1-mini",
            "scripted-1",
        ]


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Just enough session for the loader: `execute` returns canned rows."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return _FakeResult(self._rows)


def _row(name: str, models: list[str], *, active: bool = True) -> Any:
    return SimpleNamespace(
        id=f"mpf_{name}",
        name=name,
        description="operator order",
        providers=[{"model": m, "provider": "openrouter", "weight": 1.0} for m in models],
        max_latency_ms=60_000,
        max_retries=2,
        requires_tool_calling=False,
        requires_structured_output=False,
        max_classification="public",
        fallback_profile_id=None,
        is_active=active,
    )


def _models(load: Any, name: str) -> list[str]:
    return [c.model for c in load.profiles[name].candidates]


class TestPersistedExplicitOrderWins:
    async def test_an_explicit_nvidia_first_row_beats_the_builtin_dots_first(
        self,
    ) -> None:
        """The loader overlays by name: a tenant that persisted NVIDIA-first
        keeps NVIDIA-first, even though the builtin default is now DOTS-first."""
        load = await load_profiles_from_session(
            _FakeSession([_row("primary", [NVIDIA, DOTS])]), "org_test"
        )
        assert load.rows == 1
        assert _models(load, "primary") == [NVIDIA, DOTS]

    async def test_no_rows_means_builtin_dots_first(self) -> None:
        load = await load_profiles_from_session(_FakeSession([]), "org_test")
        assert _models(load, "primary")[0] == DOTS

    async def test_an_inactive_row_changes_nothing(self) -> None:
        load = await load_profiles_from_session(
            _FakeSession([_row("primary", [NVIDIA, DOTS], active=False)]), "org_test"
        )
        assert _models(load, "primary")[0] == DOTS
