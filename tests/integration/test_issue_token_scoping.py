"""Operator token issuance must select a tenant before looking up its user."""

import importlib.util
from pathlib import Path

import jwt
import pytest
from sqlalchemy import select

from ai_orchestrator.config.settings import get_settings, reset_settings_cache
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.models import Organization, User

pytestmark = [pytest.mark.integration, pytest.mark.security]
spec = importlib.util.spec_from_file_location(
    "operator_issue_token", Path(__file__).resolve().parents[2] / "scripts/issue_token.py"
)
issuer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issuer)
KEY = "synthetic-operator-token-tests-never-deploy"


async def member(tenant, email, **changes):
    user = User(
        id="usr_" + new_ulid(),
        organization_id=tenant.organization_id,
        email=email,
        display_name="Synthetic operator bootstrap",
        role="admin",
        is_org_admin=True,
        is_privileged=True,
        **changes,
    )
    tenant.session.add(user)
    await tenant.commit()


def settings(monkeypatch):
    monkeypatch.setenv("AO_JWT_SECRET", KEY)
    reset_settings_cache()


async def selected_slug(tenant):
    return await tenant.session.scalar(
        select(Organization.slug).where(Organization.id == tenant.organization_id)
    )


async def test_same_email_in_two_tenants_issues_only_the_explicit_tenant(
    tenant, other_tenant, monkeypatch, capsys
):
    settings(monkeypatch)
    email = "bootstrap-" + new_ulid() + "@example.test"
    await member(tenant, email)
    await member(other_tenant, email)
    result = await issuer.issue(email, await selected_slug(tenant))
    assert result == 0
    output = capsys.readouterr().out
    token = output.strip().splitlines()[-1]
    claims = jwt.decode(token, KEY, algorithms=["HS256"], audience="ai-orchestrator-api")
    assert claims["org"] == tenant.organization_id
    assert claims["org"] != other_tenant.organization_id


async def test_ambiguous_organization_requires_explicit_selection(tenant, monkeypatch, capsys):
    settings(monkeypatch)
    email = "bootstrap-" + new_ulid() + "@example.test"
    await member(tenant, email)
    assert await issuer.issue(email, None) != 0
    assert "--org" in capsys.readouterr().err


@pytest.mark.parametrize("changes", [{"is_active": False}, {"mfa_enabled": True}])
async def test_inactive_or_mfa_user_is_not_issued_a_bypass_token(
    tenant, monkeypatch, capsys, changes
):
    settings(monkeypatch)
    email = "bootstrap-" + new_ulid() + "@example.test"
    await member(tenant, email, **changes)
    assert await issuer.issue(email, await selected_slug(tenant)) != 0
    assert "eyJ" not in capsys.readouterr().out


async def test_missing_signing_key_fails_without_issuing_token(tenant, monkeypatch, capsys):
    from pydantic import SecretStr

    settings(monkeypatch)
    configured = get_settings().model_copy(update={"jwt_secret": SecretStr("")})
    monkeypatch.setattr(issuer, "get_settings", lambda: configured)
    email = "bootstrap-" + new_ulid() + "@example.test"
    await member(tenant, email)
    assert await issuer.issue(email, await selected_slug(tenant)) != 0
    assert "eyJ" not in capsys.readouterr().out
