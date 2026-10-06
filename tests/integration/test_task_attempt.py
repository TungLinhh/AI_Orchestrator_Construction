"""Lease and cancellation are observed from independent PostgreSQL sessions."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.task_attempt import TaskAttemptRunner
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.budget import TokenUsage
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.errors import ConflictError, PreconditionError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Agent, Execution, ModelUsage, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class PausedRuntime:
    name = "paused-attempt-fixture"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.execution_id = None

    async def execute(self, task, context, *, record_usage=None, **kwargs):
        self.execution_id = str(task.execution_id)
        assert self.execution_id == str(context.task.execution_id)
        if record_usage:
            await record_usage(
                ModelResponse(
                    provider="unit-fake",
                    model_used="attempt-fixture",
                    usage=TokenUsage(input_tokens=13, output_tokens=5),
                )
            )
        self.started.set()
        await self.release.wait()
        return AgentResult(
            task_id=str(task.task_id),
            execution_id=str(task.execution_id),
            status=AgentResultStatus.COMPLETED,
            summary="Verified synthetic evidence",
            output={"finding": "Synthetic artifact; no external action performed"},
        )


@pytest_asyncio.fixture
async def prepared(tenant):
    org = await tenant.session.get(Organization, tenant.organization_id)
    await seed(tenant.session, into=org)
    agent = (
        await tenant.session.scalars(
            select(Agent).where(
                Agent.organization_id == tenant.organization_id,
                Agent.name == "Finance Agent",
            )
        )
    ).one()
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="Observe a durable task attempt",
        goal="Inspect synthetic independent evidence and report a substantiated finding",
        task_type="analysis",
        owner_agent_id=agent.id,
    )
    task_id = task.id
    await tenant.commit()
    return tenant.organization_id, task_id


async def rows(db, org, task_id):
    async with db.tenant_session(org) as session:
        task = await session.get(Task, task_id)
        executions = list(
            (
                await session.scalars(
                    select(Execution).where(
                        Execution.organization_id == org,
                        Execution.task_id == task_id,
                    )
                )
            ).all()
        )
        usages = list(
            (
                await session.scalars(
                    select(ModelUsage).where(
                        ModelUsage.organization_id == org,
                        ModelUsage.task_id == task_id,
                    )
                )
            ).all()
        )
        return task, executions, usages


async def start(db, prepared, runtime, *, heartbeat=None):
    org, task_id = prepared
    worker = asyncio.create_task(
        TaskAttemptRunner(db, org, runtime=runtime, heartbeat=heartbeat).execute_task(task_id)
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=5)
    return worker


async def test_claim_usage_and_heartbeat_visible_while_runtime_waits(db, prepared, monkeypatch):
    import ai_orchestrator.application.task_attempt as attempt

    monkeypatch.setattr(
        attempt,
        "get_settings",
        lambda: get_settings().model_copy(
            update={
                "task_execution_lease_s": 15,
                "task_execution_heartbeat_s": 1,
            }
        ),
    )
    runtime = PausedRuntime()
    renewed = asyncio.Event()
    pulses = 0

    def observe(task, execution):
        nonlocal pulses
        pulses += 1
        if pulses >= 3:
            renewed.set()

    worker = await start(db, prepared, runtime, heartbeat=observe)
    try:
        org, task_id = prepared
        task, executions, usages = await rows(db, org, task_id)
        assert task.status == "running" and task.lease_expires_at > utcnow()
        assert len(executions) == 1 and executions[0].id == runtime.execution_id
        assert executions[0].status == "running"
        assert len(usages) == 1 and usages[0].input_tokens == 13
        first = task.lease_expires_at
        await asyncio.wait_for(renewed.wait(), timeout=5)
        assert (await rows(db, org, task_id))[0].lease_expires_at > first
        # Updating the row on another connection cannot wait for the runtime.
        async with db.tenant_session(org) as session:
            await asyncio.wait_for(
                session.execute(
                    Task.__table__.update().where(Task.id == task_id).values(priority="high")
                ),
                timeout=1,
            )
        runtime.release.set()
        assert (await asyncio.wait_for(worker, timeout=5)).status.value == "completed"
        task, executions, usages = await rows(db, org, task_id)
        assert task.status == "completed" and task.lease_expires_at is None
        assert task.priority == "high" and executions[0].status == "completed"
    finally:
        runtime.release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_two_runners_cannot_bill_the_same_task(db, prepared):
    runtime = PausedRuntime()
    worker = await start(db, prepared, runtime)
    try:
        org, task_id = prepared
        with pytest.raises(ConflictError, match="Another worker"):
            await TaskAttemptRunner(db, org, runtime=PausedRuntime()).execute_task(task_id)
        assert len((await rows(db, org, task_id))[1]) == 1
        runtime.release.set()
        await worker
        # A completed task is read back, never called again or billed again.
        outcome = await TaskAttemptRunner(db, org, runtime=PausedRuntime()).execute_task(task_id)
        assert outcome.status.value == "completed"
        assert len((await rows(db, org, task_id))[1]) == 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_cancel_does_not_wait_for_model_and_late_result_cannot_complete(db, prepared):
    runtime = PausedRuntime()
    worker = await start(db, prepared, runtime)
    org, task_id = prepared
    try:
        async with db.tenant_session(org) as session:
            await asyncio.wait_for(
                TaskRepository(session, org).transition(task_id, Transition.CANCEL),
                timeout=1,
            )
        runtime.release.set()
        result = (await asyncio.gather(worker, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        task, executions, usages = await rows(db, org, task_id)
        assert task.status == "canceled" and not task.output
        assert executions[0].status == "failed"
        assert executions[0].error_category == "attempt_interrupted"
        assert usages[0].execution_id == executions[0].id
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_shutdown_preserves_usage_and_blocks_unsafe_replay(db, prepared):
    runtime = PausedRuntime()
    worker = await start(db, prepared, runtime)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    org, task_id = prepared
    task, executions, usages = await rows(db, org, task_id)
    assert task.status == "blocked" and task.constraints["reconciliation_required"]
    assert executions[0].status == "failed" and usages[0].input_tokens == 13
    with pytest.raises(PreconditionError, match="paused"):
        await TaskAttemptRunner(db, org, runtime=PausedRuntime()).execute_task(task_id)
    assert len((await rows(db, org, task_id))[1]) == 1


async def test_database_cancel_interrupts_the_runtime_on_next_heartbeat(db, prepared, monkeypatch):
    import ai_orchestrator.application.task_attempt as attempt

    monkeypatch.setattr(
        attempt,
        "get_settings",
        lambda: get_settings().model_copy(
            update={
                "task_execution_heartbeat_s": 1,
            }
        ),
    )
    runtime = PausedRuntime()
    worker = await start(db, prepared, runtime)
    org, task_id = prepared
    try:
        async with db.tenant_session(org) as session:
            await TaskRepository(session, org).transition(task_id, Transition.CANCEL)
        async with asyncio.timeout(5):
            result = (await asyncio.gather(worker, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert (await rows(db, org, task_id))[0].status == "canceled"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_kill_leaves_visible_claim_and_usage_without_blind_replay(db, prepared, tmp_path):
    org, task_id = prepared
    ready, log = tmp_path / "attempt.json", tmp_path / "attempt.log"
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.integration.task_attempt_crash_process",
            org,
            task_id,
            str(ready),
            stdout=output,
            stderr=output,
        )
        try:
            async with asyncio.timeout(15):
                while not ready.exists():
                    assert process.returncode is None, log.read_text()
                    await asyncio.sleep(0.05)
            proof = json.loads(ready.read_text())
            task, executions, usages = await rows(db, org, task_id)
            assert task.status == "running" and task.lease_expires_at > utcnow()
            assert executions[0].id == proof["execution_id"] and len(usages) == 1
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    # The advisory lock is released by death, but absence of a worker does not
    # prove absence of an external effect. Refuse rather than fabricate recovery.
    with pytest.raises(PreconditionError, match="unfinished attempt"):
        await TaskAttemptRunner(db, org, runtime=PausedRuntime()).execute_task(task_id)
    assert len((await rows(db, org, task_id))[1]) == 1


async def test_worker_factory_callable_uses_the_same_durable_owner(db, prepared):
    from ai_orchestrator.worker_runtime import build_execution_service

    org, task_id = prepared
    factory = build_execution_service(org, database=db)
    runtime = PausedRuntime()
    factory.runtime = runtime
    assert isinstance(factory(), TaskAttemptRunner)
    work = asyncio.create_task(factory.execute_task(task_id))
    try:
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        task, executions, usages = await rows(db, org, task_id)
        assert task.status == "running" and len(executions) == 1 and len(usages) == 1
        runtime.release.set()
        assert (await work).status.value == "completed"
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_heartbeat_failure_stops_runtime_and_is_reported(db, prepared):
    settings = get_settings().model_copy(update={"task_execution_heartbeat_s": 1})
    runtime = PausedRuntime()
    pulses = 0

    def broken_pulse(task, execution):
        nonlocal pulses
        pulses += 1
        if pulses >= 3:
            raise RuntimeError("heartbeat transport unavailable")

    org, task_id = prepared
    worker = asyncio.create_task(
        TaskAttemptRunner(
            db,
            org,
            runtime=runtime,
            settings=settings,
            heartbeat=broken_pulse,
        ).execute_task(task_id)
    )
    try:
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        with pytest.raises(PreconditionError, match="heartbeat failed"):
            await asyncio.wait_for(worker, timeout=5)
        task, executions, usages = await rows(db, org, task_id)
        assert task.status == "blocked" and len(usages) == 1
        assert executions[0].error_message == "RuntimeError: heartbeat transport unavailable"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_transaction_scoped_caller_cannot_bypass_active_attempt(db, prepared):
    from ai_orchestrator.application.task_execution import TaskExecutionService

    runtime = PausedRuntime()
    worker = await start(db, prepared, runtime)
    org, task_id = prepared
    try:
        async with db.tenant_session(org) as session:
            with pytest.raises(PreconditionError, match="active execution"):
                await TaskExecutionService(
                    session,
                    org,
                    runtime=PausedRuntime(),
                ).execute_task(task_id)
        assert len((await rows(db, org, task_id))[1]) == 1
        runtime.release.set()
        await worker
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_claim_commit_failure_does_not_mask_the_original_error(db, prepared, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    async def unavailable_commit(session):
        raise RuntimeError("claim commit unavailable")

    runtime = PausedRuntime()
    org, task_id = prepared
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", unavailable_commit)
        with pytest.raises(RuntimeError, match="claim commit unavailable"):
            await TaskAttemptRunner(db, org, runtime=runtime).execute_task(task_id)
    task, executions, usages = await rows(db, org, task_id)
    assert not runtime.started.is_set()
    assert task.status in {"created", "assigned"}
    assert not executions and not usages
