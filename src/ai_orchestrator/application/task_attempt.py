"""Transaction owner for a general task attempt, shared by native and Temporal.

An advisory transaction lock excludes another runner without locking task rows
through a slow runtime call. The claim, usage and internal tool records commit
at explicit checkpoints. A separate tenant session renews a fenced work lease.
An interrupted general attempt is blocked for inspection, not replayed blindly:
connector receipts are needed before external writes can safely be retried.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select, text, update

from ai_orchestrator.application.task_execution import ExecutionOutcome, TaskExecutionService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import Settings, get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, TaskStatus
from ai_orchestrator.domain.errors import ConflictError, PreconditionError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Execution, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


class AttemptStopped(asyncio.CancelledError):
    """Ownership changed; no late model result may mutate the task."""


class TaskAttemptRunner:
    def __init__(
        self,
        database: Database,
        organization_id: str,
        *,
        runtime: Any,
        heartbeat: Callable[[str, str], None] | None = None,
        settings: Settings | None = None,
        **service_options: Any,
    ) -> None:
        self.db = database
        self.org = organization_id
        self.runtime = runtime
        self.heartbeat = heartbeat
        self.settings = settings
        self.options = service_options

    async def execute_task(self, task_id: str, **kwargs: Any) -> ExecutionOutcome:
        settings = self.settings or get_settings()
        lease = settings.task_execution_lease_s
        interval = min(settings.task_execution_heartbeat_s, lease / 3)
        execution_id: str | None = None
        monitor: asyncio.Task[None] | None = None
        owner = asyncio.current_task()
        assert owner is not None
        monitor_error: BaseException | None = None
        params = {"org": self.org, "key": "task-attempt:" + task_id}
        async with self.db.engine.begin() as lock:
            acquired = await lock.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
                params,
            )
            if not acquired:
                raise ConflictError("Another worker owns this task attempt")
            async with self.db.committing_tenant_session(self.org) as session:
                repo = TaskRepository(session, self.org)
                task = await repo.get(task_id)
                if task.status in {"completed", "failed", "canceled", "expired"}:
                    return ExecutionOutcome(
                        task_id=task_id,
                        status=TaskStatus(task.status),
                        summary=task.last_error or "This task has already settled",
                    )
                previous = await session.scalar(
                    select(Execution.id)
                    .where(
                        Execution.organization_id == self.org,
                        Execution.task_id == task_id,
                        Execution.status == "running",
                    )
                    .limit(1)
                )
                if previous or task.status == "running":
                    raise PreconditionError(
                        "An unfinished attempt requires inspection before retry",
                        details={"execution_id": previous},
                    )
                if task.status not in {"created", "queued", "assigned", "waiting_for_approval"}:
                    raise PreconditionError(
                        "This task is paused; resolve its input or review first"
                    )
                highest = await session.scalar(
                    select(func.max(Execution.attempt)).where(
                        Execution.organization_id == self.org, Execution.task_id == task_id
                    )
                )
                kwargs["attempt"] = max(int(kwargs.get("attempt", 1)), int(highest or 0) + 1)

                async def renew() -> None:
                    nonlocal monitor_error
                    try:
                        while True:
                            await asyncio.sleep(interval)
                            async with self.db.tenant_session(self.org) as pulse:
                                renewed = await pulse.scalar(
                                    update(Task)
                                    .where(
                                        Task.organization_id == self.org,
                                        Task.id == task_id,
                                        Task.status == "running",
                                        select(Execution.id)
                                        .where(
                                            Execution.organization_id == self.org,
                                            Execution.id == execution_id,
                                            Execution.task_id == task_id,
                                            Execution.status == "running",
                                        )
                                        .exists(),
                                    )
                                    .values(
                                        lease_expires_at=func.now()
                                        + func.make_interval(0, 0, 0, 0, 0, 0, lease),
                                        updated_at=func.now(),
                                    )
                                    .returning(Task.id)
                                )
                                if renewed is None:
                                    raise AttemptStopped(
                                        "Task or execution no longer owns the lease"
                                    )
                            if self.heartbeat and execution_id:
                                self.heartbeat(task_id, execution_id)
                    except asyncio.CancelledError as exc:
                        if isinstance(exc, AttemptStopped):
                            monitor_error = exc
                            owner.cancel()
                        raise
                    except Exception as exc:
                        monitor_error = exc
                        owner.cancel()

                async def checkpoint(current_id: str, *, final: bool = False) -> None:
                    nonlocal execution_id, monitor
                    execution_id = current_id
                    # Do not autoflush a late result before checking ownership.
                    with session.no_autoflush:
                        state = (
                            await session.execute(
                                select(Task.status, Execution.status)
                                .join(Execution, Execution.task_id == Task.id)
                                .where(
                                    Task.organization_id == self.org,
                                    Task.id == task_id,
                                    Execution.organization_id == self.org,
                                    Execution.id == current_id,
                                )
                                .with_for_update(of=Task)
                            )
                        ).one_or_none()
                    if state != ("running", "running"):
                        raise AttemptStopped("Task was canceled or attempt ownership changed")
                    if final:
                        # Keep this short lock through finalization and commit;
                        # otherwise a Cancel could race the last ownership check.
                        return
                    await session.commit()
                    await self.db.bind_tenant(session, self.org)
                    if monitor is None:
                        monitor = asyncio.create_task(renew(), name="task-lease-" + task_id)
                    if self.heartbeat:
                        self.heartbeat(task_id, current_id)

                async def usage_checkpoint() -> None:
                    if execution_id:
                        await checkpoint(execution_id)

                service = TaskExecutionService(
                    session,
                    self.org,
                    runtime=self.runtime,
                    execution_checkpoint=checkpoint,
                    model_usage_checkpoint=usage_checkpoint,
                    lease_seconds=lease,
                    **self.options,
                )
                try:
                    outcome = await service.execute_task(task_id, **kwargs)
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                        monitor = None
                    await session.execute(
                        update(Task)
                        .where(Task.organization_id == self.org, Task.id == task_id)
                        .values(lease_expires_at=None)
                    )
                    await session.commit()
                    return outcome
                except BaseException as exc:
                    await session.rollback()
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                        monitor = None
                    if execution_id:
                        await self._interrupt(task_id, execution_id, monitor_error or exc)
                    if monitor_error and not isinstance(monitor_error, AttemptStopped):
                        raise PreconditionError("Task lease heartbeat failed") from monitor_error
                    raise
                finally:
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)

    async def _interrupt(self, task_id: str, execution_id: str, cause: BaseException) -> None:
        async with self.db.tenant_session(self.org) as session:
            repo = TaskRepository(session, self.org)
            task = await repo.get(task_id)
            execution = await session.get(Execution, execution_id)
            if execution and execution.status == "running":
                execution.status = "failed"
                execution.finished_at = utcnow()
                execution.error_category = "attempt_interrupted"
                execution.error_message = type(cause).__name__ + ": " + str(cause)[:1000]
            if task.status == "running":
                await repo.transition(task_id, Transition.BLOCK)
                task.last_error = (
                    "Attempt interrupted; inspect committed usage and tool records before retry"
                )
                task.constraints = {
                    **task.constraints,
                    "attempt_interrupted": execution_id,
                    "reconciliation_required": True,
                }
            task.lease_expires_at = None
            await AuditService(session, self.org).record(
                actor=Actor(id="task-attempt-runner", kind=ActorType.SYSTEM),
                action="task.attempt.interrupted",
                resource_type="task",
                resource_id=task_id,
                task_id=task_id,
                execution_id=execution_id if execution else None,
                outcome="failure",
                context={"error_type": type(cause).__name__},
            )
