"""The controller persists every step, stops at real gates and never hides failures."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.business_workflow import BusinessWorkflowService, create_workflow
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.workflow_fixtures import hiring_fixture, procurement_fixture
from ai_orchestrator.approvals.service import ApprovalService
from ai_orchestrator.domain.contracts import Actor, AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.models import Approval, AuditLog, Organization, Task, User
from ai_orchestrator.persistence.process import SopDefinition
from ai_orchestrator.persistence.repositories.task import ExecutionRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class HiringFixtureRuntime:
    """Explicit fake content for exercising every native hiring stage offline."""

    name = "workflow_evidence"

    def __init__(self, stage, validator):
        self.stage = stage
        self.validator = validator

    async def execute(self, task, context, *, record_usage=None, **kwargs):
        from ai_orchestrator.domain.business_workflow import RUBRIC_SPEC
        from ai_orchestrator.domain.hiring_process import JD_SECTIONS

        prior = task.input["prior"]
        key = self.stage.key
        if key == "jd":
            output = {
                "sections": {
                    name: "Synthetic MEP engineering duties and reviewed evidence."
                    for name in JD_SECTIONS
                },
                "requirements": ["Verified MEP engineering experience"],
                "kpis": ["Clash reports reviewed by the technical owner"],
            }
        elif key == "rubric":
            output = {
                "criteria": [
                    {
                        "key": k,
                        "max_points": w,
                        "description": "MEP criterion",
                        "good": "Independent verified experience",
                        "average": "Supervised",
                        "poor": "Limited experience",
                        "evidence_required": "Exact CV quote",
                    }
                    for k, w in RUBRIC_SPEC
                ],
                "threshold": 70,
                "missing_evidence_rule": "Unverified evidence scores zero",
            }
        elif key == "scoring":
            output = {
                "candidates": [
                    {
                        "candidate_id": cv["candidate_id"],
                        "criteria": [
                            {
                                "key": k,
                                "level": "good"
                                if cv["filename"] == "mep-test-a.txt"
                                else "missing",
                                "evidence_quote": cv["text"]
                                if cv["filename"] == "mep-test-a.txt"
                                else "",
                                "reason": "Synthetic reference; model quality unmeasured",
                            }
                            for k, _ in RUBRIC_SPEC
                        ],
                        "strengths": ["Evidence checked"],
                        "gaps": ["Verify during interview"],
                        "interview_questions": ["Demonstrate a commissioning calculation"],
                    }
                    for cv in prior["cv_intake"]["cvs"]
                ]
            }
        elif key == "selection":
            candidate = next(
                cv for cv in prior["cv_intake"]["cvs"] if cv["filename"] == "mep-test-a.txt"
            )
            output = {
                "recommended_candidate_id": candidate["candidate_id"],
                "rationale": "Verified two synthetic interviews and rubric",
                "conditions": ["Boss must review"],
                "alternatives": [],
            }
        elif key == "offer":
            output = {
                "candidate_id": prior["selection"]["recommended_candidate_id"],
                "offer_draft": "SYNTHETIC offer draft; not sent",
                "contract_draft": "SYNTHETIC contract draft; not signed",
                "salary": 28000000,
                "start_date": task.input["brief"]["start_date"],
                "conditions": [],
            }
        else:
            assert key == "onboarding_plan"
            output = {
                day: [
                    {
                        "owner": "HR Agent",
                        "deliverable": "Mentor review",
                        "acceptance": "Actual evidence at the due date",
                    }
                ]
                for day in ("day_one", "day_30", "day_60", "day_90")
            }
            output.update(
                {
                    "mentor": "Synthetic mentor",
                    "training": ["Sandbox HSE induction"],
                    "access_request": {
                        "workspace": "staging-mep",
                        "permissions": ["read"],
                        "production_access": False,
                    },
                }
            )
        self.validator(output)
        if record_usage:
            await record_usage(ModelResponse(provider="unit-fake", model_used="hiring-reference"))
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            task_id=task.task_id,
            execution_id=task.execution_id,
            summary="Synthetic stage exercised",
            output=output,
        )


async def test_all_hiring_stages_run_offline_with_actual_sandbox_files(
    prepared, db, tmp_path, monkeypatch
):
    from ai_orchestrator.application import business_workflow
    from ai_orchestrator.integrations import recruitment_mail

    monkeypatch.setattr(business_workflow, "DEV_DATA_DIR", tmp_path)
    monkeypatch.setattr(recruitment_mail, "DEV_DATA_DIR", tmp_path)
    records = {}

    class FileDropMailbox:
        def __init__(self, org):
            self.org = org

        def send_tests(self, root, cvs):
            records[root] = [
                {
                    **recruitment_mail.store_cv(
                        cv["text"].encode(), cv["filename"], self.org, root
                    ),
                    "synthetic": True,
                }
                for cv in cvs
            ]
            return {
                "sent": [{"sha256": cv["sha256"]} for cv in records[root]],
                "transport": "mock-file-drop",
                "synthetic": True,
            }

        def read_cvs(self, root):
            return {
                "cvs": records[root],
                "count": len(records[root]),
                "readonly": True,
                "messages": [],
                "rejected": [],
                "protocol": "mock-file-drop",
            }

    root = await create_workflow(
        prepared.session, prepared.organization_id, "mep_hiring", "simulation", hiring_fixture()
    )
    await prepared.commit()
    service = BusinessWorkflowService(
        db,
        prepared.organization_id,
        runtime_factory=HiringFixtureRuntime,
        mailbox_factory=FileDropMailbox,
    )
    report = await service.run(root)
    assert report["status"] == "completed", report
    assert report["completed"] == report["total"] == 21
    assert report["summary"]["simulated_reviews"] == 7
    assert report["summary"]["human_approvals"] == 0
    assert report["evidence"]["real_model_calls"] == 0
    assert report["evidence"]["fake_model_calls"] == 6
    assert [c["score"] for c in report["summary"]["candidates"]] == [100, 0, 0]
    assert report["summary"]["future_day_30_60_90_completed"] is False
    assert report["summary"]["onboarding"]["production_access"] is False
    folder = tmp_path / "recruitment" / prepared.organization_id / root / "onboarding"
    assert len(list(folder.glob("*.json"))) == 5
    assert all(a["readback_verified"] for a in report["summary"]["onboarding"]["actions"])
    assert (await service.run(root))["completed"] == 21
    async with db.tenant_session(prepared.organization_id) as session:
        assert (
            not (
                await session.execute(
                    select(Approval).where(Approval.organization_id == prepared.organization_id)
                )
            )
            .scalars()
            .all()
        )


async def test_operator_creates_live_hiring_without_synthetic_evidence(client, prepared, db):
    from tests.integration.api_client import auth_headers
    from tests.integration.test_console_management import _human_headers

    brief = {
        "position": "Kỹ sư MEP",
        "boss_brief": (
            "HVAC engineering and BIM coordination, source evidence and human review required."
        ),
        "salary_min": 20000000,
        "salary_max": 30000000,
        "start_date": "2026-11-01",
    }
    denied = await client.post(
        "/api/v1/workflows/hiring", json=brief, headers=auth_headers(prepared.organization_id)
    )
    assert denied.status_code == 403
    headers = await _human_headers(prepared)
    for invalid in ({"start_date": "2026-02-30"}, {"salary_max": 1}):
        response = await client.post(
            "/api/v1/workflows/hiring", json={**brief, **invalid}, headers=headers
        )
        assert response.status_code in {400, 422}, response.text
    response = await client.post("/api/v1/workflows/hiring", json=brief, headers=headers)
    assert response.status_code == 201, response.text
    async with db.tenant_session(prepared.organization_id) as session:
        root = await session.get(Task, response.json()["id"])
        assert root.input["mode"] == "live"
        assert root.input["brief"]["synthetic"] is False
        assert "test_cvs" not in root.input["brief"]
    report = await client.get("/api/v1/workflows/" + root.id, headers=headers)
    assert report.status_code == 200
    assert "test_mail" not in {s["key"] for s in report.json()["stages"]}
    assert report.json()["completed"] == 0


@pytest_asyncio.fixture
async def prepared(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    for code in ("ONX-MO-PRC-SOP-005", "ONX-MO-PRC-SOP-006", "ONX-BO-HR-SOP-004"):
        tenant.session.add(
            SopDefinition(
                id="sop_" + code + tenant.organization_id[-5:],
                organization_id=tenant.organization_id,
                code=code,
                name_vi="Test catalogue",
                block="BO" if "BO-HR" in code else "MO",
                department="HR" if "BO-HR" in code else "PRC",
                owner_role_key="procurement_lead",
                source="human",
            )
        )
    await tenant.commit()
    return tenant


class FixtureRuntime:
    name = "workflow_evidence"

    def __init__(self, stage, validator):
        self.stage = stage
        self.validator = validator

    async def execute(self, task, context, *, record_usage=None, **kwargs):
        brief = task.input["brief"]
        assert not {
            "test_cvs",
            "interview_technical",
            "interview_hr",
            "offer_acceptance",
            "onboarding_evidence",
            "delivery",
        }.intersection(brief)
        key = self.stage.key
        if key == "plan":
            output = {
                "material_plan": [
                    {k: m[k] for k in ("material_id", "quantity", "technical_spec", "long_lead")}
                    for m in brief["boq"]
                ],
                "schedule": ["Order pump before G3; ten-day pipe/cable lead time"],
                "missing_information": [],
            }
        elif key == "rfq":
            output = {
                "rfq": "Draft RFQ, not sent externally",
                "supplier_ids": [s["supplier_id"] for s in brief["suppliers"]],
                "quote_register": [
                    {
                        "supplier_id": s["supplier_id"],
                        **{k: q[k] for k in ("material_id", "quantity", "unit_price")},
                    }
                    for s in brief["suppliers"]
                    for q in s["quotes"]
                ],
            }
        elif key == "qualification":
            output = {
                "suppliers": [
                    {
                        "supplier_id": s["supplier_id"],
                        "classification": "green" if s["legal_valid"] else "red",
                        "reason": "Legal evidence checked",
                        "legal_review": s["legal_ref"],
                        "financial_review": s["financial_ref"],
                        "hse_review": s["hse_ref"],
                    }
                    for s in brief["suppliers"]
                ]
            }
        elif key == "quality":
            output = {
                "assessments": [
                    {
                        "supplier_id": s["supplier_id"],
                        "material_id": q["material_id"],
                        "accepted": q["spec_compliant"],
                        "certificate_ref": q["certificate_ref"],
                        "reason": "Exact technical/certificate fixture checked",
                    }
                    for s in brief["suppliers"]
                    for q in s["quotes"]
                ],
                "missing_evidence": ["NCC-C has no valid certificates"],
            }
        elif key == "comparison":
            awards = []
            for m in brief["boq"]:
                valid = [
                    (s, q)
                    for s in brief["suppliers"]
                    if s["legal_valid"]
                    for q in s["quotes"]
                    if q["material_id"] == m["material_id"] and q["spec_compliant"]
                ]
                s, q = min(valid, key=lambda row: row[1]["unit_price"])
                awards.append(
                    {
                        "supplier_id": s["supplier_id"],
                        **{k: q[k] for k in ("material_id", "quantity", "unit_price")},
                    }
                )
            output = {
                "comparison": [
                    {
                        "supplier_id": s["supplier_id"],
                        "material_id": q["material_id"],
                        "total_price": q["quantity"] * q["unit_price"],
                        "delivery_days": q["delivery_days"],
                        "warranty_months": q["warranty_months"],
                        "eligible": q["spec_compliant"] and s["legal_valid"],
                        "reason": "Compared quote and legal/quality evidence",
                    }
                    for s in brief["suppliers"]
                    for q in s["quotes"]
                ],
                "recommended_awards": awards,
                "reasons": [
                    "Choose the lowest compliant quote; preserve delivery and warranty requirements"
                ],
            }
        else:
            output = {
                "negotiation_record": ["Proposal only; no vendor agreement claimed"],
                "commercial_conditions": ["Keep quoted price without a signed revised quotation"],
                "risks": [],
            }
        self.validator(output)
        if record_usage:
            await record_usage(
                ModelResponse(provider="unit-fake", model_used="fixture-not-real-model")
            )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="Verified unit fixture",
            task_id=task.task_id,
            execution_id=task.execution_id,
            output=output,
        )


async def new_run(prepared, mode="simulation", brief=None):
    root = await create_workflow(
        prepared.session,
        prepared.organization_id,
        "procurement",
        mode,
        brief or procurement_fixture(),
    )
    await prepared.commit()
    return root


async def test_full_simulation_logs_system_reviews_and_preserves_no_real_approval(prepared, db):
    root = await new_run(prepared)
    service = BusinessWorkflowService(db, prepared.organization_id, runtime_factory=FixtureRuntime)
    report = await service.run(root)
    assert report["status"] == "completed", report
    assert report["completed"] == report["total"] == 13
    assert report["summary"]["simulated_reviews"] == 2
    assert report["summary"]["human_approvals"] == 0
    assert report["evidence"]["real_model_calls"] == 0
    assert report["evidence"]["fake_model_calls"] == 6
    assert report["summary"]["three_way_match"]["matched"] is True
    async with db.tenant_session(prepared.organization_id) as session:
        assert (
            not (
                await session.execute(
                    select(Approval).where(Approval.organization_id == prepared.organization_id)
                )
            )
            .scalars()
            .all()
        )
        reviews = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == prepared.organization_id,
                        AuditLog.action == "workflow.review.simulated",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(reviews) == 2
        assert {r.actor_type for r in reviews} == {"system"}
    assert (await service.run(root))["completed"] == 13


async def test_live_run_stops_before_material_quality_approval_and_ordinary_run_cannot_bypass(
    prepared, db
):
    root = await new_run(prepared, "live")
    service = BusinessWorkflowService(db, prepared.organization_id, runtime_factory=FixtureRuntime)
    report = await service.run(root)
    assert report["status"] == "blocked", report
    gate = next(s for s in report["stages"] if s["key"] == "material_review")
    assert gate["status"] == "waiting_for_approval"
    assert next(s for s in report["stages"] if s["key"] == "comparison")["status"] == "assigned"
    async with db.tenant_session(prepared.organization_id) as session:
        with pytest.raises(PreconditionError, match="controller"):
            await TaskExecutionService(
                session,
                prepared.organization_id,
                runtime=type("Ordinary", (), {"name": "ordinary"})(),
            ).execute_task(gate["id"])
    again = await service.run(root)
    assert again["completed"] == report["completed"]
    assert again["status"] == "blocked"


async def test_independent_invoice_mismatch_fails_and_cancels_remaining_work(prepared, db):
    brief = procurement_fixture()
    for row in brief["delivery"]["invoice_lines"]:
        row["unit_price"] += 1
    root = await new_run(prepared, brief=brief)
    report = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FixtureRuntime
    ).run(root)
    assert report["status"] == "failed"
    assert next(s for s in report["stages"] if s["key"] == "three_way_match")["status"] == "failed"
    assert next(s for s in report["stages"] if s["key"] == "close")["status"] == "canceled"
    assert all(s["status"] in {"completed", "failed", "canceled"} for s in report["stages"])


async def test_report_cannot_cross_tenants(prepared, other_tenant, db):
    root = await new_run(prepared)
    with pytest.raises(NotFoundError):
        await BusinessWorkflowService(db, other_tenant.organization_id).report(root)


async def test_execution_finish_after_reload_handles_numeric_zero_and_increment(prepared, db):
    root = await new_run(prepared)
    async with db.tenant_session(prepared.organization_id) as session:
        row = await ExecutionRepository(session, prepared.organization_id).start(
            task_id=root, agent_id=None, runtime_adapter="probe", model_profile="none"
        )
        eid = str(row.id)
    async with db.tenant_session(prepared.organization_id) as session:
        row = await ExecutionRepository(session, prepared.organization_id).finish(
            eid, status="completed", cost_usd=0.001
        )
        assert float(row.cost_usd) == 0.001


async def test_failed_run_reuses_only_validated_artifacts_from_identical_brief(prepared, db):
    brief = procurement_fixture()
    for row in brief["delivery"]["invoice_lines"]:
        row["unit_price"] += 1
    first = await new_run(prepared, brief=brief)
    old = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FixtureRuntime
    ).run(first)
    assert old["status"] == "failed"
    async with db.tenant_session(prepared.organization_id) as session:
        second = await create_workflow(
            session,
            prepared.organization_id,
            "procurement",
            "simulation",
            brief,
            reuse_source=first,
        )
    result = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FixtureRuntime
    ).run(second)
    assert result["status"] == "failed"  # same bad invoice cannot be repaired by reuse
    assert result["evidence"]["fake_model_calls"] == 0
    reused = [s for s in result["stages"] if s.get("reused_from")]
    assert len(reused) == 6
    assert next(s for s in result["stages"] if s["key"] == "three_way_match")["status"] == "failed"


@pytest.mark.parametrize("tamper", [False, True])
async def test_real_human_review_resumes_only_the_exact_approved_artifact(prepared, db, tamper):
    root = await new_run(prepared, "live")
    service = BusinessWorkflowService(db, prepared.organization_id, runtime_factory=FixtureRuntime)
    report = await service.run(root)
    gate = next(s for s in report["stages"] if s["key"] == "material_review")
    async with db.tenant_session(prepared.organization_id) as session:
        approval = (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == prepared.organization_id,
                    Approval.task_id == gate["id"],
                )
            )
        ).scalar_one()
        human = (
            (
                await session.execute(
                    select(User).where(
                        User.organization_id == prepared.organization_id,
                        User.is_privileged.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert human is not None
        await ApprovalService(session, prepared.organization_id).decide(
            approval.id,
            approver=Actor(id=human.id, kind=ActorType.HUMAN, is_privileged_human=True),
            approve=True,
            note="Integration-only review of the exact quality artifact",
        )
        if tamper:
            quality_id = next(s["id"] for s in report["stages"] if s["key"] == "quality")
            quality = await session.get(Task, quality_id)
            quality.output = {**quality.output, "missing_evidence": ["Altered after decision"]}
    resumed = await service.run(root)
    next_gate = next(s for s in resumed["stages"] if s["key"] == "award_review")
    if tamper:
        assert resumed["status"] == "failed", resumed
        assert next_gate["status"] == "canceled"
    else:
        assert resumed["status"] == "blocked", resumed
        reviewed = next(s for s in resumed["stages"] if s["key"] == "material_review")
        assert reviewed["output"]["human_decision"] is True
        assert reviewed["output"]["decided_by"] == human.id
        assert next_gate["status"] == "waiting_for_approval"
        assert next(s for s in resumed["stages"] if s["key"] == "po")["status"] == "assigned"


async def test_failed_runtime_leaves_no_running_execution(prepared, db):
    class BrokenRuntime(FixtureRuntime):
        async def execute(self, *args, **kwargs):
            raise ValueError("Deliberate provider artifact failure")

    root = await new_run(prepared)
    report = await BusinessWorkflowService(
        db,
        prepared.organization_id,
        runtime_factory=BrokenRuntime,
    ).run(root)
    assert report["status"] == "failed"
    from ai_orchestrator.persistence.models import Execution

    async with db.tenant_session(prepared.organization_id) as session:
        ids = [root, *(s["id"] for s in report["stages"])]
        running = (
            await session.execute(
                select(Execution.id).where(
                    Execution.organization_id == prepared.organization_id,
                    Execution.task_id.in_(ids),
                    Execution.status == "running",
                )
            )
        ).all()
        assert not running


async def test_same_brief_does_not_reuse_artifact_whose_source_input_changed(prepared, db):
    brief = procurement_fixture()
    for row in brief["delivery"]["invoice_lines"]:
        row["unit_price"] += 1
    first = await new_run(prepared, brief=brief)
    report = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FixtureRuntime
    ).run(first)
    async with db.tenant_session(prepared.organization_id) as session:
        comparison = await session.get(
            Task, next(s["id"] for s in report["stages"] if s["key"] == "comparison")
        )
        comparison.input = {
            **comparison.input,
            "prior": {"brief": {"boss_brief": "Different source"}},
        }
        retry = await create_workflow(
            session,
            prepared.organization_id,
            "procurement",
            "simulation",
            brief,
            reuse_source=first,
        )
    retried = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FixtureRuntime
    ).run(retry)
    comparison = next(s for s in retried["stages"] if s["key"] == "comparison")
    assert comparison["status"] == "completed"
    assert comparison["reused_from"] is None
    assert retried["evidence"]["fake_model_calls"] == 1
    assert retried["status"] == "failed"


async def test_cancel_after_a_recorded_model_call_settles_every_execution(prepared, db):
    import asyncio

    from ai_orchestrator.persistence.models import Execution

    class InterruptedRuntime(FixtureRuntime):
        async def execute(self, task, context, *, record_usage=None, **kwargs):
            await record_usage(
                ModelResponse(provider="unit-fake", model_used="interrupted-fixture")
            )
            raise asyncio.CancelledError("Test interruption after ledger checkpoint")

    root = await new_run(prepared)
    service = BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=InterruptedRuntime
    )
    with pytest.raises(asyncio.CancelledError):
        await service.run(root)
    report = await service.report(root)
    assert report["status"] == "blocked"
    assert report["lifecycle"]["state"] == "suspended"
    assert report["evidence"]["fake_model_calls"] == 1
    async with db.tenant_session(prepared.organization_id) as session:
        running = (
            await session.execute(
                select(Execution.id).where(
                    Execution.organization_id == prepared.organization_id,
                    Execution.task_id.in_([root, *(s["id"] for s in report["stages"])]),
                    Execution.status == "running",
                )
            )
        ).all()
        assert not running


async def test_generic_task_controls_preserve_business_workflow_boundaries(client, prepared, db):
    from tests.integration.test_console_management import _human_headers

    class FailedRuntime(FixtureRuntime):
        async def execute(self, *args, **kwargs):
            raise ValueError("Deliberate acceptance failure")

    root = await new_run(prepared)
    report = await BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=FailedRuntime
    ).run(root)
    assert report["status"] == "failed"
    headers = await _human_headers(prepared)
    blocked = await client.post("/api/v1/tasks/" + root + "/run", headers=headers)
    assert blocked.status_code == 409
    task_report = await client.get("/api/v1/tasks/" + root + "/report", headers=headers)
    assert task_report.json()["task"]["workflow_id"] == root
    retried = await client.post("/api/v1/tasks/" + root + "/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["business_workflow"] is True
    new_id = retried.json()["task_id"]
    fresh = await client.get("/api/v1/workflows/" + new_id, headers=headers)
    assert fresh.status_code == 200
    assert fresh.json()["total"] == 13
    assert all(s["status"] == "assigned" for s in fresh.json()["stages"])
    repeated = await client.post("/api/v1/tasks/" + root + "/retry", headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["task_id"] == new_id
    assert repeated.json()["already_active"] is True
    first_stage = fresh.json()["stages"][0]["id"]
    denied_cancel = await client.post("/api/v1/tasks/" + first_stage + "/cancel", headers=headers)
    assert denied_cancel.status_code == 409
    canceled = await client.post("/api/v1/tasks/" + new_id + "/cancel", headers=headers)
    assert canceled.status_code == 200, canceled.text
    closed = await client.get("/api/v1/workflows/" + new_id, headers=headers)
    assert closed.json()["status"] == "canceled"
    assert all(s["status"] == "canceled" for s in closed.json()["stages"])


async def test_cancel_interrupts_an_inflight_workflow_without_waiting_for_model(
    client, prepared, db
):
    import asyncio

    from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
    from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
    from tests.integration.test_console_management import _human_headers

    entered = asyncio.Event()

    class PausedRuntime(FixtureRuntime):
        async def execute(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    root = await new_run(prepared)
    headers = await _human_headers(prepared)

    async def execute(org, root_id, kind):
        await BusinessWorkflowService(db, org, runtime_factory=PausedRuntime).run(root_id)

    drivers = WorkflowDrivers(db, handler=execute)
    await client._transport.app.state.workflow_drivers.shutdown()
    client._transport.app.state.workflow_drivers = drivers
    assert drivers.start(prepared.organization_id, root, WorkflowKind.BUSINESS)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        canceled = await asyncio.wait_for(
            client.post("/api/v1/tasks/" + root + "/cancel", headers=headers), timeout=5
        )
        assert canceled.status_code == 200, canceled.text
        closed = await client.get("/api/v1/workflows/" + root, headers=headers)
        assert closed.json()["status"] == "canceled"
        assert all(s["status"] in {"completed", "canceled"} for s in closed.json()["stages"])
    finally:
        await drivers.shutdown()


@pytest.mark.parametrize("no_shortlist", [True, False])
async def test_hiring_waits_for_a_revision_when_no_candidate_can_be_selected(
    prepared, db, tmp_path, monkeypatch, no_shortlist
):
    from ai_orchestrator.application import business_workflow
    from ai_orchestrator.integrations import recruitment_mail

    monkeypatch.setattr(business_workflow, "DEV_DATA_DIR", tmp_path)
    monkeypatch.setattr(recruitment_mail, "DEV_DATA_DIR", tmp_path)
    records, calls = {}, []

    class Mailbox:
        def __init__(self, org):
            self.org = org

        def send_tests(self, root, cvs):
            records[root] = [
                {
                    **recruitment_mail.store_cv(
                        cv["text"].encode(), cv["filename"], self.org, root
                    ),
                    "synthetic": True,
                }
                for cv in cvs
            ]
            return {
                "sent": [{"sha256": cv["sha256"]} for cv in records[root]],
                "transport": "mock-file-drop",
                "synthetic": True,
            }

        def read_cvs(self, root):
            return {
                "cvs": records[root],
                "readonly": True,
                "messages": [],
                "rejected": [],
                "protocol": "mock-file-drop",
            }

    class Runtime(HiringFixtureRuntime):
        async def execute(self, task, context, **kwargs):
            calls.append(self.stage.key)
            if self.stage.key == "scoring" and no_shortlist:
                original = self.validator

                def validator(output):
                    levels = {
                        "mechanical": "average",
                        "electrical": "good",
                        "coordination": "average",
                        "commissioning": "average",
                        "hse": "average",
                        "documentation": "poor",
                    }
                    for criterion in output["candidates"][0]["criteria"]:
                        criterion["level"] = levels[criterion["key"]]
                    original(output)

                self.validator = validator
            return await super().execute(task, context, **kwargs)

    brief = hiring_fixture()
    if not no_shortlist:
        brief["interview_hr"]["transcripts"][0]["result"] = "fail"
    root = await create_workflow(
        prepared.session, prepared.organization_id, "mep_hiring", "simulation", brief
    )
    await prepared.commit()
    service = BusinessWorkflowService(
        db, prepared.organization_id, runtime_factory=Runtime, mailbox_factory=Mailbox
    )
    report = await service.run(root)
    assert report["status"] == "blocked", report
    assert report["waiting"]["code"] == (
        "no_eligible_candidates" if no_shortlist else "no_interview_qualified_candidates"
    )
    assert report["waiting"]["highest_score"] == (68 if no_shortlist else 100)
    assert report["waiting"]["threshold"] == 70
    active = next(stage for stage in report["stages"] if stage["status"] != "completed")
    assert active["key"] == ("interview_technical" if no_shortlist else "selection")
    assert active["status"] == "waiting_for_input"
    assert "selection" not in calls and "offer" not in calls
    assert report["completed"] == (10 if no_shortlist else 12)
    scores = next(stage["output"] for stage in report["stages"] if stage["key"] == "scoring")
    before = list(calls)
    async with db.tenant_session(prepared.organization_id) as session:
        executions = await ExecutionRepository(session, prepared.organization_id).list_for_task(
            root
        )
        execution_count = len(executions)
    again = await service.run(root)
    assert calls == before
    assert again["waiting"] == report["waiting"]
    assert next(stage["output"] for stage in again["stages"] if stage["key"] == "scoring") == scores
    async with db.tenant_session(prepared.organization_id) as session:
        assert (
            len(await ExecutionRepository(session, prepared.organization_id).list_for_task(root))
            == execution_count
        )
        audit = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.task_id == active["id"],
                    AuditLog.action == "workflow.hiring.awaiting_candidates",
                )
            )
        ).all()
        assert len(audit) == 1
