"""Model fixtures exercise real RLS, ledger, approval hashes and automatic setup."""

import copy

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.agent_blueprints import AgentBlueprintService
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.agent_blueprint import AgentBlueprint
from ai_orchestrator.domain.contracts import Actor, AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import AuthorizationError, PreconditionError, ValidationError
from ai_orchestrator.models.gateway import ModelCandidate, ModelGateway, ModelProfile
from ai_orchestrator.persistence.models import Agent, Approval, Event, Organization, Task, User
from ai_orchestrator.persistence.process import SopDefinition
from ai_orchestrator.seed import seed
from tests.integration.test_console_management import _human_headers
from tests.unit.test_agent_blueprint import example_plan

pytestmark = pytest.mark.integration


class FixtureRuntime:
    name = "agent_blueprint"

    def __init__(self, gateway, validator, **kwargs):
        self.validator = validator
        self.checkpoint = kwargs.get("checkpoint")

    async def execute(self, task, context, **kwargs):
        if self.checkpoint:
            await self.checkpoint()
        output = (
            example_plan()
            if task.input.get("agent_blueprint_draft")
            else {"report": "Source-backed test artifact"}
        )
        if task.input.get("agent_blueprint_draft"):
            for step in output["steps"]:
                step["source_sop_codes"] = [
                    sop["code"] for sop in task.input["config"]["source_sops"]
                ]
            output["steps"][0]["source_step_refs"] = [
                ref["ref"]
                for sop in task.input["config"]["source_sops"]
                for ref in sop.get("source_steps", [])
            ]
        self.validator(output)
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            task_id=task.task_id,
            execution_id=task.execution_id,
            summary="Test fixture only",
            output=output,
        )


@pytest_asyncio.fixture
async def prepared(tenant, monkeypatch):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    tenant.session.add(
        SopDefinition(
            id="sop_" + tenant.organization_id[-20:],
            organization_id=tenant.organization_id,
            code="ONX-BO-HR-SOP-004",
            name_vi="Recruitment test reference",
            block="BO",
            department="HR",
            owner_role_key="hr_lead",
            source="human",
        )
    )
    await tenant.commit()
    headers = await _human_headers(tenant)
    owner = (
        await tenant.session.execute(
            select(Agent).where(
                Agent.organization_id == tenant.organization_id, Agent.name == "HR Agent"
            )
        )
    ).scalar_one()
    user = (
        await tenant.session.execute(
            select(User).where(
                User.organization_id == tenant.organization_id,
                User.email == "console-review@example.test",
            )
        )
    ).scalar_one()
    actor = Actor(id=user.id, kind=ActorType.HUMAN, is_privileged_human=True)

    async def gateway(*args, **kwargs):
        return ModelGateway(
            profiles={
                "primary": ModelProfile(
                    name="primary",
                    candidates=(ModelCandidate(provider="openrouter", model="test/model:free"),),
                )
            }
        )

    monkeypatch.setattr(
        "ai_orchestrator.application.agent_blueprints.build_tenant_gateway", gateway
    )
    monkeypatch.setattr(
        "ai_orchestrator.application.agent_blueprints.BlueprintRuntime", FixtureRuntime
    )
    config = {
        "name": "Prepared HR agent",
        "department": "hr",
        "definition_id": owner.definition_id,
        "model_profile": "primary",
        "mandate": "Prepare every HR department procedure from source evidence",
    }
    return tenant, headers, actor, config


async def test_edit_invalidates_review_and_approval_provisions_all_tasks(client, prepared):
    tenant, headers, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    assert draft.output["revision"] == 1
    with pytest.raises(PreconditionError, match="blueprint controller"):
        await TaskExecutionService(
            tenant.session, tenant.organization_id, runtime=ScriptedRuntime()
        ).execute_task(draft.id)
    pending = await service.submit(draft.id)
    old_id = pending.id
    plan = copy.deepcopy(draft.output["plan"])
    plan["description"] = "Boss edited this exact description"
    await service.edit(draft.id, AgentBlueprint.model_validate(plan), actor, revision=1)
    assert pending.status == "expired"
    with pytest.raises(PreconditionError, match="revision changed"):
        await service.edit(draft.id, AgentBlueprint.model_validate(plan), actor, revision=1)
    with pytest.raises(PreconditionError):
        await service.provision(old_id, actor)
    new_review = await service.submit(draft.id)
    assert (await service.submit(draft.id)).id == new_review.id
    new_id = new_review.id
    await tenant.commit()
    response = await client.post(
        "/api/v1/approvals/" + new_id + "/approve", headers=headers, json={}
    )
    assert response.status_code == 200, response.text
    result = response.json()["provisioned"]
    await tenant.db.bind_tenant(tenant.session, tenant.organization_id)
    tenant.session.expire_all()
    agent = await tenant.session.get(Agent, result["agent_id"])
    root = await tenant.session.get(Task, result["workflow_id"])
    assert agent.lifecycle_status == "active"
    assert agent.description == plan["description"]
    assert agent.autonomy_level == "l2_parent_review"
    assert root.status == "waiting_for_input"
    assert len(root.input["step_ids"]) == 3
    assert len(result["review_task_ids"]) == 1
    assert (await service.provision(new_id, actor)) == result
    with pytest.raises(PreconditionError, match="workflow controller"):
        await TaskExecutionService(
            tenant.session, tenant.organization_id, runtime=ScriptedRuntime()
        ).execute_task(root.id)


async def test_owned_workflow_waits_for_sources_and_real_human_gate(prepared):
    tenant, _, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root = await service.tasks.get(result["workflow_id"])
    assert (await service.run(root.id))["missing_inputs"] == ["brief"]
    root.input = {**root.input, "inputs": {"brief": "Source input supplied by Boss"}}
    paused = await service.run(root.id)
    assert paused["status"] == "blocked"
    review = await service.approvals.get(paused["approval_id"])
    assert review.status == "pending"
    assert (
        len(
            (
                await tenant.session.execute(
                    select(Approval).where(Approval.organization_id == tenant.organization_id)
                )
            )
            .scalars()
            .all()
        )
        == 2
    )
    await service.approvals.decide(review.id, approver=actor, approve=True)
    assert (await service.run(root.id))["status"] == "completed"
    events = (
        (
            await tenant.session.execute(
                select(Event.type).where(Event.organization_id == tenant.organization_id)
            )
        )
        .scalars()
        .all()
    )
    assert events.count("approval.requested") == 2
    assert events.count("approval.decided") == 2


async def test_review_hash_refuses_changed_stage_and_cross_tenant(prepared, other_tenant):
    tenant, _, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root = await service.tasks.get(result["workflow_id"])
    root.input = {**root.input, "inputs": {"brief": "Source evidence"}}
    paused = await service.run(root.id)
    await service.approvals.decide(paused["approval_id"], approver=actor, approve=True)
    child = await service.tasks.get(result["task_ids"][-1])
    child.output = {"report": "Modified after review"}
    with pytest.raises(AuthorizationError, match="different payload"):
        await service.run(root.id)
    with pytest.raises(ValidationError, match="not found"):
        await AgentBlueprintService(other_tenant.session, other_tenant.organization_id).get(
            draft.id
        )


@pytest.mark.parametrize("decision,final_status", [("approve", "completed"), ("reject", "failed")])
async def test_api_run_and_review_automatically_resume_owned_workflow(
    client, prepared, decision, final_status
):
    import asyncio

    tenant, headers, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    approval = await service.submit(draft.id)
    with pytest.raises(PreconditionError, match="approved human"):
        await service.provision(approval.id, actor)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root_id = result["workflow_id"]
    await tenant.commit()
    prefix = "/api/v1/agent-blueprints/workflows/" + root_id
    response = await client.post(
        prefix + "/inputs",
        headers=headers,
        json={"inputs": {"brief": "Actual test source evidence"}},
    )
    assert response.status_code == 200, response.text
    assert (await client.post(prefix + "/run", headers=headers, json={})).status_code == 202
    for _ in range(100):
        async with tenant.db.tenant_session(tenant.organization_id) as session:
            root = await session.get(Task, root_id)
            if root.status == "blocked":
                break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("The driver never reached its human gate")
    async with tenant.db.tenant_session(tenant.organization_id) as session:
        review = (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == tenant.organization_id,
                    Approval.action_type == "agent.workflow.review",
                    Approval.status == "pending",
                )
            )
        ).scalar_one()
        review_id = review.id
    response = await client.post(
        "/api/v1/approvals/" + review_id + "/" + decision, headers=headers, json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow_started"] is True
    for _ in range(100):
        async with tenant.db.tenant_session(tenant.organization_id) as session:
            root = await session.get(Task, root_id)
            if root.status == final_status:
                break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("The approval did not resume the owned workflow")
    assert (
        await client.post(
            prefix + "/inputs", headers=headers, json={"inputs": {"brief": "Changed source"}}
        )
    ).status_code == 409


async def test_cancel_interrupts_model_and_preserves_owned_tree(client, prepared, monkeypatch):
    import asyncio

    from ai_orchestrator.api.agent_blueprints import _RUNS
    from ai_orchestrator.persistence.models import Execution

    tenant, headers, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root_id = result["workflow_id"]
    await tenant.commit()
    entered = asyncio.Event()

    class PausedRuntime(FixtureRuntime):
        async def execute(self, task, context, **kwargs):
            await self.checkpoint()
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "ai_orchestrator.application.agent_blueprints.BlueprintRuntime", PausedRuntime
    )
    prefix = "/api/v1/agent-blueprints/workflows/" + root_id
    await client.post(
        prefix + "/inputs", headers=headers, json={"inputs": {"brief": "Real test sources"}}
    )
    response = await client.post(prefix + "/run", headers=headers, json={})
    assert response.status_code == 202, response.text
    await asyncio.wait_for(entered.wait(), timeout=3)
    child_id = result["task_ids"][0]
    assert (
        await client.post("/api/v1/tasks/" + child_id + "/cancel", headers=headers)
    ).status_code == 409
    response = await asyncio.wait_for(
        client.post("/api/v1/tasks/" + root_id + "/cancel", headers=headers), timeout=3
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "canceled"
    assert (tenant.organization_id, root_id) not in _RUNS
    async with tenant.db.tenant_session(tenant.organization_id) as session:
        rows = (
            (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == tenant.organization_id,
                        Task.parent_task_id == root_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows and all(t.status == "canceled" for t in rows)
        running = (
            (
                await session.execute(
                    select(Execution).where(
                        Execution.organization_id == tenant.organization_id,
                        Execution.status == "running",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert not running


async def test_previous_output_is_wired_into_next_step(prepared):
    tenant, _, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    plan = copy.deepcopy(draft.output["plan"])
    plan["steps"][1]["input_keys"] = ["report"]
    await service.edit(draft.id, AgentBlueprint.model_validate(plan), actor, revision=1)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root = await service.tasks.get(result["workflow_id"])
    root.input = {**root.input, "inputs": {"brief": "Source evidence"}}
    paused = await service.run(root.id)
    child = await service.tasks.get(result["task_ids"][1])
    assert child.input["inputs"] == {"report": "Source-backed test artifact"}
    assert child.input["prior"]["step_1"] == {"report": "Source-backed test artifact"}
    assert paused["status"] == "blocked"


@pytest.mark.parametrize(
    "department,agent_name,source_department,code",
    [
        ("sales", "Sales Agent", "BD", "ONX-FO-BD-SOP-001"),
        ("qa", "Quality Agent", "HSE", "ONX-MO-HSE-SOP-008"),
    ],
)
async def test_department_drafts_include_mapped_source_procedures(
    prepared, department, agent_name, source_department, code
):
    tenant, _, _, config = prepared
    owner = (
        await tenant.session.execute(
            select(Agent).where(
                Agent.organization_id == tenant.organization_id, Agent.name == agent_name
            )
        )
    ).scalar_one()
    definition_id = owner.definition_id
    tenant.session.add(
        SopDefinition(
            id="sop_" + tenant.organization_id[-20:] + source_department,
            organization_id=tenant.organization_id,
            code=code,
            name_vi="Mapped source reference",
            block="FO" if department == "sales" else "MO",
            department=source_department,
            owner_role_key="hr_lead",
            source="human",
        )
    )
    await tenant.commit()
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(
        {**config, "department": department, "definition_id": definition_id}
    )
    assert draft.status == "completed"
    assert [s["code"] for s in draft.input["config"]["source_sops"]] == [code]
    assert draft.input["config"]["source_sops"][0]["source_steps"]
    assert all("IT" not in c for s in draft.output["plan"]["steps"] for c in s["source_sop_codes"])


async def test_missing_source_step_edit_is_a_client_error_without_invalidating_review(
    client, prepared
):
    tenant, headers, _, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    review = await service.submit(draft.id)
    draft_id, review_id = draft.id, review.id
    plan = copy.deepcopy(draft.output["plan"])
    plan["steps"][0]["source_step_refs"] = []
    await tenant.commit()
    response = await client.patch(
        "/api/v1/agent-blueprints/" + draft_id, headers=headers, json={"revision": 1, "plan": plan}
    )
    assert response.status_code == 422, response.text
    assert "every supplied source step" in response.text
    unchanged = await client.get("/api/v1/agent-blueprints/" + draft_id, headers=headers)
    assert unchanged.json()["output"]["revision"] == 1
    assert unchanged.json()["approvals"] == [{"id": review_id, "status": "pending"}]


async def test_sources_arrive_at_their_stage_without_rewriting_completed_evidence(client, prepared):
    tenant, headers, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    plan = copy.deepcopy(draft.output["plan"])
    plan["required_inputs"]["interview_notes"] = "Human interview notes available after screening"
    plan["steps"][1]["input_keys"] = ["report", "interview_notes"]
    await service.edit(draft.id, AgentBlueprint.model_validate(plan), actor, revision=1)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    root_id = result["workflow_id"]
    await tenant.commit()
    prefix = "/api/v1/agent-blueprints/workflows/" + root_id
    response = await client.post(
        prefix + "/inputs", headers=headers, json={"inputs": {"brief": "Original Boss source"}}
    )
    assert response.status_code == 200, response.text
    await tenant.db.bind_tenant(tenant.session, tenant.organization_id)
    tenant.session.expire_all()
    paused = await service.run(root_id)
    assert paused["status"] == "waiting_for_input"
    assert paused["waiting_step"] == "step_2"
    assert paused["missing_inputs"] == ["interview_notes"]
    first = await service.tasks.get(result["task_ids"][0])
    assert first.status == "completed"
    first_output = copy.deepcopy(first.output)
    first_id = first.id
    await tenant.commit()
    detail = (await client.get(prefix, headers=headers)).json()
    assert detail["inputs_locked"] is True
    assert detail["missing_inputs"] == ["interview_notes"]
    response = await client.post(
        prefix + "/inputs",
        headers=headers,
        json={"inputs": {"brief": "Rewritten historical source"}},
    )
    assert response.status_code == 409, response.text
    response = await client.post(
        prefix + "/inputs",
        headers=headers,
        json={"inputs": {"interview_notes": "Human interview evidence"}},
    )
    assert response.status_code == 200, response.text
    await tenant.db.bind_tenant(tenant.session, tenant.organization_id)
    tenant.session.expire_all()
    paused = await service.run(root_id)
    assert paused["status"] == "blocked"
    first = await service.tasks.get(first_id)
    assert first.output == first_output
    second = await service.tasks.get(result["task_ids"][1])
    assert second.input["inputs"] == {
        "report": "Source-backed test artifact",
        "interview_notes": "Human interview evidence",
    }
    await service.approvals.decide(paused["approval_id"], approver=actor, approve=True)
    assert (await service.run(root_id))["status"] == "completed"


async def test_request_information_does_not_reject_the_temporal_workflow(
    client, prepared, monkeypatch
):
    from unittest.mock import AsyncMock

    from ai_orchestrator.approvals.service import ApprovalRequest

    tenant, headers, _, _ = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    review = await service.approvals.create(
        ApprovalRequest(
            organization_id=tenant.organization_id,
            action_type="test.review",
            action_payload={"artifact": "Needs explanation"},
            requested_by="fixture-agent",
            workflow_id="temporal-review-test",
            reason="Explain the source evidence",
        )
    )
    review_id = review.id
    await tenant.commit()
    signal = AsyncMock(return_value=True)
    monkeypatch.setattr("ai_orchestrator.workflows.client.signal_approval_decision", signal)
    response = await client.post(
        "/api/v1/approvals/" + review_id + "/request-information",
        headers=headers,
        json={"note": "Please attach the original evidence"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_information"
    assert response.json()["workflow_signalled"] is False
    signal.assert_not_awaited()
    await tenant.db.bind_tenant(tenant.session, tenant.organization_id)
    tenant.session.expire_all()
    unchanged = await service.approvals.get(review_id)
    assert unchanged.status == "needs_information"
    assert unchanged.decision_note == "Please attach the original evidence"


async def test_local_runner_refuses_owned_tasks_before_scheduling(prepared):
    from ai_orchestrator.application import local_runner

    tenant, _, actor, config = prepared
    service = AgentBlueprintService(tenant.session, tenant.organization_id, tenant.db)
    draft = await service.draft(config)
    approval = await service.submit(draft.id)
    await service.approvals.decide(approval.id, approver=actor, approve=True)
    result = await service.provision(approval.id, actor)
    for task_id in [
        draft.id,
        result["workflow_id"],
        *result["task_ids"],
        *result["review_task_ids"],
    ]:
        handle = await local_runner.start(
            tenant.session, organization_id=tenant.organization_id, task_id=task_id
        )
        assert handle.started is False
        assert "workflow controller" in handle.reason
        assert (tenant.organization_id, task_id) not in local_runner._in_flight
        assert (tenant.organization_id, task_id) not in local_runner._running
    child = await service.tasks.get(result["task_ids"][0])
    assert child.status == "assigned"
