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
