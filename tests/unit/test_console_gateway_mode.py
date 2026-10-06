"""An explicit fake runtime must not inspect or call configured real adapters."""

from __future__ import annotations

import pytest

from ai_orchestrator.application.model_profiles import build_tenant_gateway


@pytest.mark.unit
async def test_fake_gateway_does_not_construct_real_provider_clients(monkeypatch):
    def forbidden(settings):
        raise AssertionError("fake mode must not construct a real provider")

    monkeypatch.setattr("ai_orchestrator.models.providers.build_providers_from_settings", forbidden)
    gateway = await build_tenant_gateway("")
    assert gateway.available_providers() == ["deterministic"]
    await gateway.aclose()


@pytest.mark.unit
async def test_real_gateway_never_substitutes_a_scripted_provider(monkeypatch):
    from ai_orchestrator.config.settings import get_settings

    settings = get_settings().model_copy(update={"model_provider_default": "openrouter"})
    monkeypatch.setattr("ai_orchestrator.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "ai_orchestrator.models.providers.build_providers_from_settings", lambda _: {}
    )
    gateway = await build_tenant_gateway("")
    assert gateway.available_providers() == []
    await gateway.aclose()
