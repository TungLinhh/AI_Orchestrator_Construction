"""Human credentials must follow the current tenant membership, not old JWT flags."""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.models import User
from ai_orchestrator.security.auth import hash_password, issue_access_token

pytestmark = [pytest.mark.integration, pytest.mark.security]
PASSWORD = "synthetic-identity-password-2026"
PASSWORD_HASH = hash_password(PASSWORD)


async def _member(tenant, *, privileged=True):
    user = User(
        id="usr_" + new_ulid(),
        organization_id=tenant.organization_id,
        email="identity-synthetic@example.test",
        display_name="Synthetic identity reviewer",
        role="admin" if privileged else "member",
        is_privileged=privileged,
        is_org_admin=privileged,
        password_hash=PASSWORD_HASH,
    )
    tenant.session.add(user)
    await tenant.commit()
    await tenant.session.refresh(user)
    token = issue_access_token(
        user_id=user.id,
        organization_id=tenant.organization_id,
        role=user.role,
        is_org_admin=user.is_org_admin,
        is_privileged=user.is_privileged,
        token_version=user.token_version,
    )
    return user, {"Authorization": "Bearer " + token}


async def test_disabled_member_cannot_keep_using_an_issued_token(client, tenant):
    user, headers = await _member(tenant)
    assert (await client.get("/api/v1/tasks", headers=headers)).status_code == 200
    user.is_active = False
    await tenant.commit()
    response = await client.get("/api/v1/tasks", headers=headers)
    assert response.status_code == 403, response.text


async def test_forced_logout_revokes_the_previous_token(client, tenant):
    user, headers = await _member(tenant)
    user.token_version += 1
    await tenant.commit()
    response = await client.get("/api/v1/tasks", headers=headers)
    assert response.status_code == 403, response.text


async def test_deleted_member_cannot_keep_using_an_issued_token(client, tenant):
    user, headers = await _member(tenant)
    await tenant.session.delete(user)
    await tenant.commit()
    response = await client.get("/api/v1/tasks", headers=headers)
    assert response.status_code == 403, response.text


async def test_a_valid_signature_does_not_grant_membership_in_another_tenant(
    client, tenant, other_tenant
):
    user, _ = await _member(tenant)
    token = issue_access_token(
        user_id=user.id,
        organization_id=other_tenant.organization_id,
        role="admin",
        is_org_admin=True,
        is_privileged=True,
        token_version=1,
    )
    response = await client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403, response.text


async def test_current_privilege_demotion_applies_to_old_tokens(client, tenant):
    user, headers = await _member(tenant)
    user.is_privileged = False
    user.is_org_admin = False
    user.role = "member"
    await tenant.commit()
    # This endpoint requires an administrator before considering any settings.
    response = await client.patch(
        f"/api/v1/organizations/{tenant.organization_id}", headers=headers, json={}
    )
    assert response.status_code == 403, response.text


async def test_human_token_is_bound_to_its_tenant_not_the_request_header(client, tenant):
    _, headers = await _member(tenant)
    headers["x-organization-id"] = "org_request_header_is_not_identity"
    response = await client.get("/api/v1/tasks", headers=headers)
    assert response.status_code == 200, response.text


async def _login(client, tenant, email="identity-synthetic@example.test", password=PASSWORD):
    return await client.post(
        "/api/v1/identity/login",
        json={"organization_id": tenant.organization_id, "email": email, "password": password},
    )


async def test_password_login_me_logout_and_relogin(client, tenant):
    await _member(tenant)
    login = await _login(client, tenant)
    assert login.status_code == 200, login.text
    assert login.headers["cache-control"] == "no-store"
    assert login.json()["expires_in"] == 1800
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    me = await client.get("/api/v1/identity/me", headers=headers)
    assert me.status_code == 200, me.text
    assert "password" not in me.text and "token_version" not in me.text
    logout = await client.post("/api/v1/identity/logout", headers=headers)
    assert logout.status_code == 200, logout.text
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 403
    assert (await _login(client, tenant)).status_code == 200


async def test_wrong_password_unknown_email_and_disabled_member_are_indistinguishable(
    client, tenant
):
    user, _ = await _member(tenant)
    wrong = await _login(client, tenant, password="wrong-synthetic-password")
    unknown = await _login(client, tenant, email="nobody@example.test")
    user.is_active = False
    await tenant.commit()
    disabled = await _login(client, tenant)
    assert wrong.status_code == unknown.status_code == disabled.status_code == 403
    assert wrong.json() == unknown.json() == disabled.json()
    assert PASSWORD not in disabled.text


async def test_mfa_member_cannot_bypass_second_factor_using_password_only(client, tenant):
    user, _ = await _member(tenant)
    user.mfa_enabled = True
    await tenant.commit()
    response = await _login(client, tenant)
    assert response.status_code == 403, response.text


async def test_password_change_revokes_old_sessions_and_old_password(client, tenant):
    _, headers = await _member(tenant)
    new_password = "replacement-synthetic-password-2026"
    response = await client.post(
        "/api/v1/identity/password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 200, response.text
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 403
    assert (await _login(client, tenant)).status_code == 403
    assert (await _login(client, tenant, password=new_password)).status_code == 200


async def test_wrong_current_password_does_not_change_credentials(client, tenant):
    _, headers = await _member(tenant)
    response = await client.post(
        "/api/v1/identity/password",
        headers=headers,
        json={"current_password": "wrong", "new_password": "replacement-synthetic-password"},
    )
    assert response.status_code == 403, response.text
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 200
    assert (await _login(client, tenant)).status_code == 200


async def test_identity_validation_never_echoes_credentials_or_extra_fields(client, tenant):
    for payload in [
        {"organization_id": tenant.organization_id, "email": "x", "password": "x" * 1025},
        {
            "organization_id": tenant.organization_id,
            "email": "x",
            "password": PASSWORD,
            "unexpected_secret": "synthetic-private-marker-DO-NOT-ECHO",
        },
    ]:
        response = await client.post("/api/v1/identity/login", json=payload)
        assert response.status_code == 422
        assert PASSWORD not in response.text
        assert "DO-NOT-ECHO" not in response.text
        assert "x" * 50 not in response.text


async def test_login_guess_limit_cannot_be_reset_by_bearer_or_forwarded_headers(client, tenant):
    for number in range(11):
        response = await client.post(
            "/api/v1/identity/login",
            headers={"Authorization": f"Bearer synthetic-{number}", "X-Forwarded-For": str(number)},
            json={"organization_id": tenant.organization_id, "email": "x", "password": "wrong"},
        )
        assert response.status_code == (403 if number < 10 else 429), response.text


async def test_admin_creates_member_and_can_revoke_then_reactivate_without_old_tokens(
    client, tenant
):
    _, admin = await _member(tenant)
    created = await client.post(
        "/api/v1/identity/members",
        headers=admin,
        json={"email": "NEW@EXAMPLE.TEST", "display_name": "New member", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text
    member_id = created.json()["id"]
    assert created.json()["email"] == "new@example.test"
    assert created.json()["is_privileged"] is False
    token = (await _login(client, tenant, email="new@example.test")).json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    response = await client.patch(
        f"/api/v1/identity/members/{member_id}", headers=admin, json={"is_active": False}
    )
    assert response.status_code == 200, response.text
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 403
    response = await client.patch(
        f"/api/v1/identity/members/{member_id}", headers=admin, json={"is_active": True}
    )
    assert response.status_code == 200, response.text
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 403
    assert (await _login(client, tenant, email="new@example.test")).status_code == 200


async def test_last_active_administrator_cannot_be_disabled_or_demoted(client, tenant):
    user, headers = await _member(tenant)
    for change in [{"is_active": False}, {"role": "member"}]:
        response = await client.patch(
            f"/api/v1/identity/members/{user.id}", headers=headers, json=change
        )
        assert response.status_code == 409, response.text


async def test_member_cannot_list_or_create_members(client, tenant):
    _, headers = await _member(tenant, privileged=False)
    assert (await client.get("/api/v1/identity/members", headers=headers)).status_code == 403
    response = await client.post(
        "/api/v1/identity/members",
        headers=headers,
        json={"email": "new@example.test", "display_name": "New", "password": PASSWORD},
    )
    assert response.status_code == 403, response.text


async def test_other_tenant_member_cannot_be_changed_or_listed(client, tenant, other_tenant):
    _, admin = await _member(tenant)
    other, _ = await _member(other_tenant)
    response = await client.patch(
        f"/api/v1/identity/members/{other.id}", headers=admin, json={"is_active": False}
    )
    assert response.status_code == 404, response.text
    listing = await client.get("/api/v1/identity/members", headers=admin)
    assert listing.status_code == 200
    assert other.id not in {row["id"] for row in listing.json()["items"]}


async def test_disabled_timestamp_is_enforced_even_if_active_flag_is_true(client, tenant):
    from datetime import UTC, datetime

    user, headers = await _member(tenant)
    user.disabled_at = datetime.now(UTC)
    await tenant.commit()
    assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 403


async def test_admin_duplicate_email_is_a_conflict_not_a_server_error(client, tenant):
    _, headers = await _member(tenant)
    response = await client.post(
        "/api/v1/identity/members",
        headers=headers,
        json={
            "email": "identity-synthetic@example.test",
            "display_name": "Duplicate",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 409, response.text


async def test_two_admins_cannot_concurrently_remove_the_last_admin(client, tenant):
    import asyncio

    first, first_headers = await _member(tenant)
    created = await client.post(
        "/api/v1/identity/members",
        headers=first_headers,
        json={
            "email": "second-admin@example.test",
            "display_name": "Second synthetic admin",
            "password": PASSWORD,
            "role": "org_admin",
        },
    )
    assert created.status_code == 201, created.text
    second_id = created.json()["id"]
    token = (await _login(client, tenant, email="second-admin@example.test")).json()["access_token"]
    second_headers = {"Authorization": "Bearer " + token}
    responses = await asyncio.gather(
        client.patch(
            f"/api/v1/identity/members/{first.id}", headers=first_headers, json={"role": "member"}
        ),
        client.patch(
            f"/api/v1/identity/members/{second_id}", headers=second_headers, json={"role": "member"}
        ),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    surviving = first_headers if responses[0].status_code == 409 else second_headers
    members = (await client.get("/api/v1/identity/members", headers=surviving)).json()["items"]
    assert sum(member["is_privileged"] and member["is_active"] for member in members) == 1


async def test_password_login_cannot_select_another_tenant_using_a_header(
    client, tenant, other_tenant
):
    await _member(tenant)
    response = await client.post(
        "/api/v1/identity/login",
        headers={"x-organization-id": other_tenant.organization_id},
        json={
            "organization_id": other_tenant.organization_id,
            "email": "identity-synthetic@example.test",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 403, response.text


async def test_service_cannot_manage_human_members_or_change_password(client, tenant):
    from tests.integration.conftest import TEST_SECRET

    headers = {
        "Authorization": "Bearer svc." + TEST_SECRET,
        "x-organization-id": tenant.organization_id,
    }
    response = await client.post(
        "/api/v1/identity/members",
        headers=headers,
        json={"email": "new@example.test", "display_name": "New", "password": PASSWORD},
    )
    assert response.status_code == 403, response.text
    assert (await client.post("/api/v1/identity/logout", headers=headers)).status_code == 403


async def test_identity_audit_never_contains_passwords_or_tokens(client, tenant):
    from sqlalchemy import select

    from ai_orchestrator.persistence.models import AuditLog

    user, headers = await _member(tenant)
    login = await _login(client, tenant)
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    await client.post("/api/v1/identity/logout", headers=headers)
    await tenant.session.refresh(user)
    rows = (await tenant.session.scalars(select(AuditLog))).all()
    actions = {row.action for row in rows}
    assert "identity.login" in actions and "identity.logout_all_sessions" in actions
    for row in rows:
        serialized = str(row.context) + str(row.policy_reason)
        assert PASSWORD not in serialized and token not in serialized


async def test_normal_identity_activity_does_not_consume_the_login_guess_budget(client, tenant):
    _, headers = await _member(tenant)
    for _ in range(11):
        response = await client.get("/api/v1/identity/me", headers=headers)
        assert response.status_code == 200, response.text
    response = await _login(client, tenant)
    assert response.status_code == 200, response.text
