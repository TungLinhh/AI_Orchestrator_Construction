"""Legacy hiring view selects a real tenant-scoped request, not demo facts."""

from uuid import uuid4

import pytest

from ai_orchestrator.application.hiring_view import hiring_process
from ai_orchestrator.domain.hiring_process import SOP_CODE, STAGES_BY_KEY
from ai_orchestrator.persistence.repositories.task import TaskRepository

pytestmark = pytest.mark.integration


async def _request(tenant, key, position, *, stage_key="need_confirmed", sop=SOP_CODE):
    repo = TaskRepository(tenant.session, tenant.organization_id)
    return await repo.create(
        title=position,
        goal=str(uuid4()),
        input={
            "request_key": key,
            "sop_code": sop,
            "stage_key": stage_key,
            "stage_n": 1,
            "hiring_request": {
                "position": position,
                "office": "Middle Office",
                "department": "Design",
                "headcount": 2,
                "jd_reference": "authorized-reference",
                "candidates": [],
            },
        },
    )


async def test_latest_non_procurement_request_and_explicit_selection(tenant):
    first = await _request(tenant, "hire:design-engineer", "Kỹ sư thiết kế")
    second = await _request(tenant, "hire:hr-specialist", "Chuyên viên nhân sự")
    await _request(tenant, "unrelated", "Not HR", sop="OTHER-SOP")
    await _request(tenant, "unknown-stage", "Unknown", stage_key="unknown")
    actual = await hiring_process(tenant.session, organization_id=tenant.organization_id)
    assert actual["request_key"] == "hire:hr-specialist"
    assert actual["position"] == "Chuyên viên nhân sự"
    assert actual["candidates"] == []
    assert actual["headcount"] == 2
    assert actual["stages"][0]["task_id"] == second.id
    selected = await hiring_process(
        tenant.session, organization_id=tenant.organization_id, request_key="hire:design-engineer"
    )
    assert selected["stages"][0]["task_id"] == first.id
    assert selected["position"] == "Kỹ sư thiết kế"
    assert actual["position"] != "Trưởng phòng Mua sắm"
    foreign = await hiring_process(tenant.session, organization_id="another-tenant")
    assert foreign["stages"] == []
    missing = await hiring_process(
        tenant.session, organization_id=tenant.organization_id, request_key="hire:not-present"
    )
    assert missing["stages"] == []
    assert missing["candidates"] == []


async def test_empty_request_never_displays_seeded_candidate_facts(tenant):
    actual = await hiring_process(tenant.session, organization_id=tenant.organization_id)
    assert actual["request_key"] is None
    assert actual["position"] == ""
    assert actual["candidates"] == []
    assert actual["headcount"] is None
    assert actual["stages"] == []


def test_cv_screening_is_owned_by_hr_back_office():
    stage = STAGES_BY_KEY["cv_screening"]
    assert (stage.office, stage.department, stage.agent_name) == ("Back Office", "HR", "HR Agent")


async def test_stage_order_uses_known_stage_not_untrusted_stage_number(tenant):
    task = await _request(tenant, "hire:custom-position", "Authorized custom position")
    task.input = {**task.input, "stage_n": "not-a-number"}
    await tenant.session.flush()
    actual = await hiring_process(tenant.session, organization_id=tenant.organization_id)
    assert actual["request_key"] == "hire:custom-position"
    assert actual["stages"][0]["task_id"] == task.id
    assert actual["stages"][0]["n"] == STAGES_BY_KEY["need_confirmed"].n


async def test_same_timestamp_attempts_select_deterministically_by_task_id(tenant):
    older = await _request(tenant, "hire:retry", "First attempt")
    newer = await _request(tenant, "hire:retry", "Second attempt")
    newer.created_at = older.created_at
    await tenant.session.flush()
    actual = await hiring_process(tenant.session, organization_id=tenant.organization_id)
    assert actual["stages"][0]["task_id"] == max(older.id, newer.id)
    assert actual["stages"][0]["attempts"] == 2
