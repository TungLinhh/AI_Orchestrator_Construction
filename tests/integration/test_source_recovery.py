"""Recovery preserves source contracts and never approves obsolete task trees."""

import datetime as dt

import pytest
from sqlalchemy import select

from ai_orchestrator.application.source_intake import SourceIntake
from ai_orchestrator.application.stale_reviews import reconcile_stale_reviews
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.task_retry import retry_failed
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, RunMode
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Approval, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.sources import SourceManifest
from ai_orchestrator.seed import seed
from tests.integration.test_approval_is_really_requested import _NeedsApprovalRuntime

pytestmark = pytest.mark.integration


async def test_retry_preserves_manifest_and_task_contract(tenant):
    intake = SourceIntake(tenant.session, tenant.organization_id)
    actor = Actor(id="source-test", kind=ActorType.HUMAN)
    files = await intake.mock("quote_comparison", actor)
    repo = TaskRepository(tenant.session, tenant.organization_id)
    task = await repo.create(
        title="Source retry",
        goal="Compare sources",
        task_type="analysis",
        input={"source_workflow": "quote_comparison"},
        budget_limit_tokens=12345,
        expected_output_schema={"type": "object"},
        constraints={"draft": True},
    )
    original = await intake.bind(task, [f.id for f in files], "mock")
    await repo.transition(task.id, Transition.FAIL)
    result = await retry_failed(
        tenant.session,
        organization_id=tenant.organization_id,
        task_id=task.id,
        now=dt.datetime.now(dt.UTC),
    )
    retry = await repo.get(result["task_id"])
    assert retry.input == task.input and retry.constraints == task.constraints
    assert retry.budget_limit_tokens == 12345
    assert retry.expected_output_schema == task.expected_output_schema
    manifest = await tenant.session.scalar(
        select(SourceManifest).where(
            SourceManifest.organization_id == tenant.organization_id,
            SourceManifest.task_id == retry.id,
        )
    )
    assert manifest.id != original.id and manifest.snapshot == original.snapshot
    assert (await intake.context(retry))["expected"]["winner"] == "A"


async def test_stale_reviews_are_expired_without_human_approval_and_idempotent(tenant):
    org = await tenant.session.get(Organization, tenant.organization_id)
    await seed(tenant.session, into=org)
    repo = TaskRepository(tenant.session, tenant.organization_id)
    parent = await repo.create(
        title="Stopped parent", goal="Stopped parent", task_type="coordination"
    )
    child = await repo.create(
        title="Child needs review", goal="Child needs review", parent_task_id=parent.id
    )
    service = TaskExecutionService(
        session=tenant.session,
        organization_id=tenant.organization_id,
        runtime=_NeedsApprovalRuntime(),
        run_mode=RunMode.SIMULATION,
        auto_approve=False,
    )
    owner = await tenant.session.scalar(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id, Agent.name == "Executive Agent"
        )
    )
    await repo.assign(child.id, owner.id)
    await service.execute_task(child.id)
    child = await repo.get(child.id)
    assert child.status == "waiting_for_approval"
    await repo.transition(parent.id, Transition.FAIL)
    actor = Actor(id="reconcile-test", kind=ActorType.SYSTEM)
    preview = await reconcile_stale_reviews(tenant.session, tenant.organization_id, actor)
    assert len(preview) == 1 and not preview[0]["applied"]
    applied = await reconcile_stale_reviews(
        tenant.session, tenant.organization_id, actor, apply=True
    )
    assert applied[0]["applied"] and child.status == "canceled"
    approvals = (
        await tenant.session.scalars(select(Approval).where(Approval.task_id == child.id))
    ).all()
    assert approvals and all(a.status == "expired" and a.decided_by is None for a in approvals)
    assert (
        await reconcile_stale_reviews(tenant.session, tenant.organization_id, actor, apply=True)
        == []
    )
