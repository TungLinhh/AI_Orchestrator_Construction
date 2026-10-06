"""Recovery is proved on committed rows and abrupt process death, not mocks of locks."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from sqlalchemy import select, text

from ai_orchestrator.application.business_workflow import BusinessWorkflowService, create_workflow
from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
from ai_orchestrator.application.workflow_fixtures import hiring_fixture
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.models import AuditLog, Execution, ModelUsage, Task
from ai_orchestrator.persistence.repositories.task import ExecutionRepository, TaskRepository
from tests.integration.test_business_workflow import FixtureRuntime, new_run
from tests.integration.test_business_workflow import prepared as business_prepared

prepared = business_prepared

pytestmark = pytest.mark.integration


async def test_abrupt_process_death_resumes_without_repeating_completed_stage(
    prepared, db, tmp_path
):
    org = prepared.organization_id
    root = await new_run(prepared)
    ready = tmp_path / "committed.json"
    log = tmp_path / "child.log"
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.integration.workflow_crash_process",
            org,
            root,
            str(ready),
            stdout=output,
            stderr=output,
        )
        try:
            async with asyncio.timeout(15):
                while not ready.exists():
                    assert process.returncode is None, log.read_text()
                    await asyncio.sleep(0.05)
            checkpoint = json.loads(ready.read_text())
            # A second connection must see the execution while the child is alive.
            async with db.tenant_session(org) as session:
                interrupted = await ExecutionRepository(session, org).get(
                    checkpoint["execution_id"]
                )
                assert interrupted.status == "running"
                assert (
                    await TaskRepository(session, org).get(checkpoint["task_id"])
                ).status == "running"
                completed = (
                    await session.scalars(
                        select(Task).where(
                            Task.organization_id == org,
                            Task.parent_task_id == root,
                            Task.status == "completed",
                        )
                    )
                ).all()
                snapshots = {t.id: t.output for t in completed}
                assert any(t.input["stage_key"] == "plan" for t in completed)
            process.kill()  # No finally or graceful cancellation can settle the child.
            await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def resume(org_id, root_id, kind):
        await BusinessWorkflowService(db, org_id, runtime_factory=FixtureRuntime).run(root_id)

    restarted = WorkflowDrivers(db, handler=resume)
    assert restarted.start(org, root, WorkflowKind.BUSINESS)
    assert not restarted.start(org, root, WorkflowKind.BUSINESS)
    await asyncio.wait_for(restarted.wait(org, root), timeout=15)
    await restarted.shutdown()
    report = await BusinessWorkflowService(db, org).report(root)
    assert report["status"] == "completed", report
    assert report["completed"] == 13
    async with db.tenant_session(org) as session:
        repo = TaskRepository(session, org)
        for task_id, output in snapshots.items():
            assert (await repo.get(task_id)).output == output
            assert len(await ExecutionRepository(session, org).list_for_task(task_id)) == 1
        interrupted = await ExecutionRepository(session, org).get(checkpoint["execution_id"])
        assert interrupted.status == "failed"
        assert interrupted.error_category == "controller_interrupted"
        attempts = await ExecutionRepository(session, org).list_for_task(checkpoint["task_id"])
        assert [e.status for e in attempts] == ["failed", "completed"]
        assert [e.attempt for e in attempts] == [1, 2]
        assert not (
            await session.scalars(
                select(Execution.id).where(
                    Execution.organization_id == org, Execution.status == "running"
                )
            )
        ).all()
        assert (
            await session.scalars(
                select(ModelUsage.id).where(
                    ModelUsage.organization_id == org, ModelUsage.execution_id == interrupted.id
                )
            )
        ).all()
        assert (
            await session.scalars(
                select(AuditLog.id).where(
                    AuditLog.organization_id == org,
                    AuditLog.task_id == root,
                    AuditLog.action == "workflow.suspended",
                )
            )
        ).all()


async def test_shutdown_suspends_and_fresh_driver_completes(prepared, db):
    entered = asyncio.Event()
    org = prepared.organization_id
    root = await new_run(prepared)

    class PausedRuntime(FixtureRuntime):
        async def execute(self, task, context, *, record_usage=None, **kwargs):
            await record_usage(ModelResponse(provider="unit-fake", model_used="shutdown-fixture"))
            entered.set()
            await asyncio.Event().wait()

    async def pause(org_id, root_id, kind):
        await BusinessWorkflowService(db, org_id, runtime_factory=PausedRuntime).run(root_id)

    drivers = WorkflowDrivers(db, handler=pause)
    assert drivers.start(org, root, WorkflowKind.BUSINESS)
    await asyncio.wait_for(entered.wait(), timeout=5)
    # Lock is shared with direct callers and survives per-stage commits.
    with pytest.raises(PreconditionError, match="already running"):
        await BusinessWorkflowService(db, org, runtime_factory=FixtureRuntime).run(root)
    await asyncio.wait_for(drivers.shutdown(), timeout=5)
    report = await BusinessWorkflowService(db, org).report(root)
    assert report["status"] == "blocked"
    assert report["lifecycle"]["state"] == "suspended"
    assert all(stage["status"] not in {"failed", "canceled"} for stage in report["stages"])
    # Transaction advisory locks must not leak into the connection pool.
    async with db.engine.begin() as lock:
        assert await lock.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:root))"),
            {"org": org, "root": root},
        )
    resumed = await BusinessWorkflowService(db, org, runtime_factory=FixtureRuntime).run(root)
    assert resumed["status"] == "completed", resumed


async def test_uncertain_mail_write_stays_blocked_without_replaying(prepared, db):
    org = prepared.organization_id
    root = await create_workflow(
        prepared.session, org, "mep_hiring", "simulation", hiring_fixture()
    )
    await prepared.commit()
    async with db.tenant_session(org) as session:
        task = (
            await session.scalars(
                select(Task).where(
                    Task.organization_id == org,
                    Task.parent_task_id == root,
                    Task.input["stage_key"].astext == "test_mail",
                )
            )
        ).one()
        task_id = task.id
        await TaskRepository(session, org).transition(task_id, Transition.BEGIN_WORK)
        execution = await ExecutionRepository(session, org).start(
            task_id=task_id,
            agent_id=task.owner_agent_id,
            runtime_adapter="mail-uncertain",
            model_profile="none",
        )
        execution_id = execution.id
        execution.input_tokens = 17
        execution.decision_record = {"receipt": "uncertain-delivery"}

    class NoMailbox:
        def __init__(self, org_id):
            pytest.fail("Uncertain mail must not be replayed")

    service = BusinessWorkflowService(
        db, org, runtime_factory=FixtureRuntime, mailbox_factory=NoMailbox
    )
    for _ in range(2):
        with pytest.raises(PreconditionError, match="reconciliation"):
            await service.run(root)
        report = await service.report(root)
        assert report["status"] == "blocked"
        assert report["lifecycle"]["reconciliation_task_ids"] == [task_id]
    async with db.tenant_session(org) as session:
        execution = await ExecutionRepository(session, org).get(execution_id)
        assert execution.input_tokens == 17
        assert execution.decision_record == {"receipt": "uncertain-delivery"}
        assert len(await ExecutionRepository(session, org).list_for_task(task_id)) == 1
