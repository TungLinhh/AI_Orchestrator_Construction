"""Operator projections must match execution and preserve exact resource scope."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.models import AgentToolBinding, Skill, SkillVersion, Tool
from tests.integration.api_client import auth_headers
from tests.integration.test_agent_control import _agent
from tests.integration.test_tenant_model_profiles import TENANT_ONLY, _write_profile

pytestmark = pytest.mark.integration


async def test_diagnostic_model_call_resolves_the_tenant_profile(client, tenant):
    await _write_profile(tenant)
    await tenant.commit()
    headers = auth_headers(tenant.organization_id)
    catalogue = (await client.get("/api/v1/model-profiles", headers=headers)).json()
    profile = next(p for p in catalogue["items"] if p["name"] == TENANT_ONLY)
    assert profile["providers"][0]["model"] == "scripted-1"
    assert any(p["name"] == "primary" for p in catalogue["items"])
    response = await client.post(
        "/api/v1/model/call",
        headers=headers,
        json={"profile": TENANT_ONLY, "prompt": "Public test", "data_classification": "public"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model_used"] == "scripted-1"
    assert response.json()["routing"]["profile"] == TENANT_ONLY


async def test_runtime_body_cannot_name_another_tenant(client, tenant, other_tenant):
    for path, body in (
        ("/model/call", {"prompt": "probe"}),
        ("/memory", {"content": "probe"}),
        ("/memory/search", {"query": "probe"}),
    ):
        response = await client.post(
            "/api/v1" + path,
            headers=auth_headers(tenant.organization_id),
            json={**body, "organization_id": other_tenant.organization_id},
        )
        assert response.status_code == 422, response.text


async def test_each_tool_reports_its_own_binding_ceiling(client, tenant):
    agent_id = await _agent(tenant)
    ids = {}
    for name, risk in (("reader", "read_only"), ("writer", "low_risk_write")):
        tool_id = "tol_" + new_ulid()
        ids[tool_id] = risk
        tenant.session.add(
            Tool(
                id=tool_id,
                organization_id=tenant.organization_id,
                name=name,
                risk_level=risk,
                effect_class="read" if risk == "read_only" else "mutate_internal",
            )
        )
        await tenant.session.flush()
        tenant.session.add(
            AgentToolBinding(
                id="atb_" + new_ulid(),
                organization_id=tenant.organization_id,
                agent_id=agent_id,
                tool_id=tool_id,
                max_risk=risk,
            )
        )
    await tenant.commit()
    response = await client.get(
        f"/api/v1/agents/{agent_id}/capabilities", headers=auth_headers(tenant.organization_id)
    )
    assert response.status_code == 200, response.text
    assert {t["id"]: t["binding_max_risk"] for t in response.json()["tools"]} == ids


async def test_skill_registry_lists_resources_once_and_details_survive_reload(client, tenant):
    skill_id = "skl_" + new_ulid()
    tenant.session.add(Skill(id=skill_id, organization_id=tenant.organization_id, name="sample"))
    await tenant.session.flush()
    for n in range(2):
        tenant.session.add(
            SkillVersion(
                id="skv_" + new_ulid(),
                organization_id=tenant.organization_id,
                skill_id=skill_id,
                version=f"1.{n}.0",
                instructions=f"revision {n}",
            )
        )
        await tenant.session.flush()
    await tenant.commit()
    headers = auth_headers(tenant.organization_id)
    page = (await client.get("/api/v1/skills?limit=1", headers=headers)).json()
    assert page["total"] == 1
    assert page["items"][0]["current_version"] == "1.1.0"
    detail = (await client.get(f"/api/v1/skills/{skill_id}", headers=headers)).json()
    assert detail["id"] == skill_id and detail["instructions"] == "revision 1"


async def test_catalogue_cannot_disclose_another_tenants_definition(client, tenant, other_tenant):
    other_agent = await _agent(other_tenant)
    own_agent = await _agent(tenant)
    assert own_agent != other_agent
    other_definition = "def_" + other_tenant.organization_id[-10:]
    response = await client.get(
        "/api/v1/console/catalogue",
        params={"kind": "definitions", "id": other_definition},
        headers=auth_headers(tenant.organization_id),
    )
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["total"] == 0


async def test_event_and_decision_pages_do_not_apply_offset_twice(client, tenant):
    agent_id = await _agent(tenant)
    for n in range(3):
        await tenant.session.execute(
            text(
                "INSERT INTO events (id, organization_id, type, source, subject, data) "
                "VALUES (:id, :org, 'console.probe', 'test', :subject, '{}'::jsonb)"
            ),
            {"id": "evt_" + new_ulid(), "org": tenant.organization_id, "subject": str(n)},
        )
    await tenant.commit()
    headers = auth_headers(tenant.organization_id)
    for n in range(3):
        await client.post(
            f"/api/v1/agents/{agent_id}/kill", headers=headers, json={"reason": f"test stop {n}"}
        )
        await client.post(f"/api/v1/agents/{agent_id}/revive", headers=headers, json={})
    for path in ("/events", "/ai-decisions"):
        first = (await client.get("/api/v1" + path, params={"limit": 1}, headers=headers)).json()
        second = (
            await client.get("/api/v1" + path, params={"limit": 1, "offset": 1}, headers=headers)
        ).json()
        assert first["total"] == second["total"] >= 3
        assert len(second["items"]) == 1
        assert first["items"][0]["id"] != second["items"][0]["id"]


async def test_negative_budget_is_rejected_at_api_boundary(client, tenant):
    agent_id = await _agent(tenant)
    response = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers(tenant.organization_id),
        json={"budget_limit_tokens": -1},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "field", ["model_profile", "runtime_adapter", "description", "capabilities"]
)
async def test_required_agent_configuration_cannot_be_cleared_to_null(client, tenant, field):
    agent_id = await _agent(tenant)
    response = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers(tenant.organization_id),
        json={field: None},
    )
    assert response.status_code == 422, response.text


async def test_unknown_model_profile_is_refused_without_changing_agent(client, tenant):
    agent_id = await _agent(tenant)
    headers = await _human_headers(tenant)
    before = (await client.get(f"/api/v1/agents/{agent_id}", headers=headers)).json()
    response = await client.patch(
        f"/api/v1/agents/{agent_id}", headers=headers, json={"model_profile": "missing-profile"}
    )
    assert response.status_code == 422, response.text
    after = (await client.get(f"/api/v1/agents/{agent_id}", headers=headers)).json()
    assert after["model_profile"] == before["model_profile"]


async def test_publication_refuses_a_version_different_from_the_reviewed_draft(client, tenant):
    headers = await _human_headers(tenant)
    created = await client.post(
        "/api/v1/skills",
        headers=headers,
        json={"name": "reviewed_draft", "instructions": "Preserve the reviewed draft."},
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    response = await client.post(
        f"/api/v1/skills/{skill['id']}/publish",
        headers=headers,
        json={"skill_version_id": "skv_" + new_ulid(), "test_results": {"passed": True}},
    )
    assert response.status_code == 422, response.text
    detail = (await client.get(f"/api/v1/skills/{skill['id']}", headers=headers)).json()
    assert detail["is_published"] is False
    assert detail["governance_state"] == "experimental"


async def _human_headers(tenant):
    from ai_orchestrator.persistence.models import User
    from ai_orchestrator.security.auth import issue_access_token

    user_id = "usr_" + new_ulid()
    tenant.session.add(
        User(
            id=user_id,
            organization_id=tenant.organization_id,
            email="console-review@example.test",
            display_name="Console reviewer",
            role="admin",
            is_org_admin=True,
            is_privileged=True,
        )
    )
    await tenant.commit()
    token = issue_access_token(
        user_id=user_id,
        organization_id=tenant.organization_id,
        role="admin",
        is_org_admin=True,
        is_privileged=True,
        token_version=1,
    )
    return {"Authorization": "Bearer " + token, "x-organization-id": tenant.organization_id}
