"""All departmental cases use durable tasks; holdout cases never become lessons."""

import pytest
import pytest_asyncio
from scripts import run_real_scenarios
from scripts.evaluate_organization import run_cases
from scripts.organization_eval import corpus
from sqlalchemy import select

from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.ids import SkillVersionId
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, AuditLog, Organization, SkillVersion
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def prepared(tenant):
    org = await tenant.session.get(Organization, tenant.organization_id)
    await seed(tenant.session, into=org)
    await tenant.commit()
    return tenant


async def test_all_departments_have_graded_tasks_and_only_training_proposals(prepared, db):
    report = await run_cases(db, prepared.organization_id, corpus(), "mock", lessons=True)
    assert report["passed"] == report["total"] == 21, report
    assert report["model_quality_measured"] is False
    assert report["production_ready"] is False
    assert all(r["execution_status"] == "completed" for r in report["results"])
    assert sum("lesson_version_id" in r for r in report["results"]) == 7
    async with db.tenant_session(prepared.organization_id) as session:
        versions = (
            (
                await session.execute(
                    select(SkillVersion).where(
                        SkillVersion.organization_id == prepared.organization_id,
                        SkillVersion.derived_from["development_only"].astext == "true",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(versions) == 7
        assert all(not v.is_published and not v.test_results for v in versions)
        assert all(v.derived_from["synthetic"] is True for v in versions)
        assert all("-normal-" in v.derived_from["case_id"] for v in versions)
        logs = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == prepared.organization_id,
                        AuditLog.action == "evaluation.graded",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 21
        assert {row.resource_id for row in logs} == {r["task_id"] for r in report["results"]}
        # Historical proposals remain available, but one training case should
        # not repeat its instructions in the next experiment's context.
        source = versions[0]
        session.add(
            SkillVersion(
                id=str(SkillVersionId.create()),
                organization_id=prepared.organization_id,
                skill_id=source.skill_id,
                version="2",
                instructions=source.instructions,
                is_published=False,
                derived_from=dict(source.derived_from),
            )
        )
        await session.commit()
    # Repeated training creates fresh attempts but reuses the same proposal.
    second = await run_cases(
        db, prepared.organization_id, corpus(), "mock", lessons=True, candidate_lessons=True
    )
    assert all(r["candidate_lesson_count"] == 1 for r in second["results"])
    assert [r.get("lesson_version_id") for r in second["results"]] == [
        r.get("lesson_version_id") for r in report["results"]
    ]


async def test_prior_tenant_result_cannot_make_new_delegation_pass(prepared, monkeypatch):
    repo = TaskRepository(prepared.session, prepared.organization_id)
    finance = (
        await prepared.session.execute(
            select(Agent).where(
                Agent.organization_id == prepared.organization_id,
                Agent.name == "Finance Agent",
            )
        )
    ).scalar_one()
    old = await repo.create(
        title="Earlier unrelated work",
        goal="Earlier unrelated work",
        task_type="analysis",
        owner_agent_id=finance.id,
    )
    await repo.assign(old.id, finance.id)
    await repo.transition(old.id, Transition.BEGIN_WORK)
    old.output = {"verdicts": "old answer", "reason": "old source"}
    await repo.transition(old.id, Transition.COMPLETE)
    await prepared.commit()

    class FailedRuntime:
        name = "deliberate-failure"

        async def execute(self, task, context, **kwargs):
            return AgentResult(
                task_id=task.task_id,
                execution_id=task.execution_id,
                status=AgentResultStatus.FAILED,
                summary="No delegation occurred.",
            )

    monkeypatch.setattr(run_real_scenarios, "_WorkingRuntime", FailedRuntime)
    report = await run_real_scenarios.run_scenario(
        prepared.session,
        prepared.organization_id,
        run_real_scenarios.SCENARIOS[0],
    )
    assert report["reached_department"] is False
    assert report["produced_output"] is False
    assert report["model_quality_verified"] is False


async def test_feedback_candidate_gets_paired_tasks_but_mock_cannot_publish(client, prepared, db):
    from scripts.evaluate_feedback_skill import compare

    from ai_orchestrator.application.skill_evaluation_gate import evaluated_feedback
    from ai_orchestrator.application.skill_learner import SkillLearner
    from ai_orchestrator.domain.errors import PreconditionError
    from tests.integration.test_console_management import _human_headers

    headers = await _human_headers(prepared)
    repo = TaskRepository(prepared.session, prepared.organization_id)
    root = await repo.create(
        title="Mock hiring campaign for learning",
        goal="Schema test only",
        input={"business_workflow": "mep_hiring"},
    )
    learner = SkillLearner(prepared.session, prepared.organization_id)
    skill_id = "skl_" + root.id[4:]
    await learner.ensure_skill(
        skill_id=skill_id, name="Mock feedback candidate", description="No real model evidence"
    )
    version = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=prepared.organization_id,
        skill_id=skill_id,
        version="1",
        instructions="Check missing sources and keep human review gates.",
        derived_from={
            "workflow_root": root.id,
            "lesson_scope": "proposal_only",
            "confirmed_by": "mock-reviewer",
        },
        is_published=False,
    )
    prepared.session.add(version)
    version_id = version.id
    await prepared.commit()
    report = await compare(db, prepared.organization_id, version_id, corpus(), "mock", rounds=2)
    assert report["receipt"]["passed"] is True
    assert len(report["receipt"]["tasks"]) == 12
    assert report["model_quality_measured"] is False
    assert report["human_expert_review_required"] is True
    assert all(
        row["report"]["candidate_lessons_used"] == (row["arm"] == "candidate")
        for row in report["reports"]
    )
    async with db.tenant_session(prepared.organization_id) as session:
        current = await session.get(SkillVersion, version_id)
        assert current.test_results["paired_evaluation"] == report["receipt"]
        with pytest.raises(PreconditionError, match="live baseline"):
            await evaluated_feedback(
                session, prepared.organization_id, current, "Mock expert source"
            )
    response = await client.post(
        f"/api/v1/skills/{skill_id}/publish",
        headers=headers,
        json={
            "skill_version_id": version_id,
            "test_results": {"passed": True},
            "security_scan": {"clean": True},
            "expert_review_source": "Mock expert source",
        },
    )
    assert response.status_code == 409, response.text
    agent = (
        await prepared.session.execute(
            select(Agent).where(
                Agent.organization_id == prepared.organization_id, Agent.name == "HR Agent"
            )
        )
    ).scalar_one()
    response = await client.post(
        f"/api/v1/agents/{agent.id}/skills",
        headers=headers,
        json={"skill_id": skill_id, "skill_version_id": version_id},
    )
    assert response.status_code == 422, response.text
    async with db.tenant_session(prepared.organization_id) as session:
        current = await session.get(SkillVersion, version_id)
        assert current.is_published is False
        assert current.derived_from.get("agent_id") is None


async def test_feedback_publication_gate_verifies_ledger_and_refuses_changed_output(prepared):
    """Constructed ledger fixtures test the gate, not real provider/model quality."""
    from ai_orchestrator.application.business_workflow import payload_hash
    from ai_orchestrator.application.skill_evaluation_gate import evaluated_feedback
    from ai_orchestrator.application.skill_learner import SkillLearner
    from ai_orchestrator.audit.service import AuditService
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.domain.enums import ActorType
    from ai_orchestrator.domain.errors import PreconditionError
    from ai_orchestrator.domain.ids import ModelUsageId
    from ai_orchestrator.persistence.models import ModelUsage, Task

    session, org = prepared.session, prepared.organization_id
    agent = (
        await session.execute(
            select(Agent).where(Agent.organization_id == org, Agent.name == "HR Agent")
        )
    ).scalar_one()
    skill_id = "skl_" + agent.id[4:]
    await SkillLearner(session, org).ensure_skill(
        skill_id=skill_id, name="Gate fixture", description="Test ledger only"
    )
    version = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=org,
        skill_id=skill_id,
        version="1",
        instructions="Gate fixture instructions",
        derived_from={"lesson_scope": "proposal_only"},
        is_published=False,
    )
    session.add(version)
    rows = []
    for iteration in range(2):
        for split in ("train", "holdout"):
            for arm in ("baseline", "candidate"):
                task = await TaskRepository(session, org).create(
                    title="[GATE FIXTURE] " + arm,
                    goal="Constructed ledger fixture, no provider called",
                    owner_agent_id=agent.id,
                    input={
                        "evaluation_case": split,
                        **(
                            {"feedback_candidate_version": version.id} if arm == "candidate" else {}
                        ),
                    },
                    constraints={"evaluation_corpus": "fixture-corpus-hash"},
                )
                await TaskRepository(session, org).transition(task.id, Transition.BEGIN_WORK)
                task.output = {"fixture": "Synthetic ledger evidence"}
                await TaskRepository(session, org).transition(task.id, Transition.COMPLETE)
                session.add(
                    ModelUsage(
                        id=str(ModelUsageId.create()),
                        organization_id=org,
                        task_id=task.id,
                        agent_id=agent.id,
                        model_profile="fixture",
                        provider="openrouter",
                        model_used="fixture/provider:free",
                        status="ok",
                    )
                )
                rows.append(
                    {
                        "round": iteration,
                        "arm": arm,
                        "split": split,
                        "case_id": split,
                        "task_id": task.id,
                        "output_hash": payload_hash(task.output),
                    }
                )
    receipt = {
        "passed": True,
        "runtime": "live",
        "rounds": 2,
        "instructions_hash": payload_hash(version.instructions),
        "agent_id": agent.id,
        "corpus_sha256": "fixture-corpus-hash",
        "tasks": rows,
    }
    version.test_results = {"paired_evaluation": receipt}
    with pytest.raises(PreconditionError, match="audit record"):
        await evaluated_feedback(session, org, version, "Constructed expert source")
    await AuditService(session, org).record(
        actor=Actor(id="gate-fixture", kind=ActorType.SYSTEM),
        action="evaluation.paired",
        resource_type="skill_version",
        resource_id=version.id,
        context={"receipt_hash": payload_hash(receipt)},
    )
    assert (await evaluated_feedback(session, org, version, "Constructed expert source"))[
        "passed"
    ] is True
    changed = await session.get(Task, rows[0]["task_id"])
    changed.output = {"fixture": "Tampered after grading"}
    await session.flush()
    with pytest.raises(PreconditionError, match="evidence changed"):
        await evaluated_feedback(session, org, version, "Constructed expert source")
