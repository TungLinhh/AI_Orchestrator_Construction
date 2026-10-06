"""Process-local dispatch and shutdown; persistent lifecycle lives in workflow_lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.application.workflow_commands import command_for, enqueue
from ai_orchestrator.config.settings import Environment, get_settings
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.models import Organization, Task, WorkflowCommand
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)
WorkflowHandler = Callable[[str, str, WorkflowKind], Awaitable[None]]


class WorkflowDrivers:
    """Owned by one application, never by API module globals.

    The registry suppresses duplicate dispatch in this process. The lifecycle's
    database lock excludes other processes and direct CLI/service callers.
    """

    def __init__(
        self,
        db: Database,
        *,
        handler: WorkflowHandler | None = None,
        concurrency: int | None = None,
        durable: bool | None = None,
    ) -> None:
        self.db = db
        self._handler = handler or self._execute
        self._runs: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._wakeups: set[tuple[str, str]] = set()
        self._closing = False
        self._durable = handler is None if durable is None else durable
        self._monitor: asyncio.Task[None] | None = None
        self._slots = asyncio.Semaphore(concurrency or get_settings().native_workflow_concurrency)

    def is_running(self, org: str, root: str) -> bool:
        task = self._runs.get((org, root))
        return task is not None and not task.done()

    def start(self, org: str, root: str, kind: WorkflowKind, *, persist: bool = True) -> bool:
        if self._closing:
            return False
        if self.is_running(org, root):
            # A human may approve just after a gate commits but before its
            # driver exits. Coalesce that wakeup instead of silently losing it.
            self._wakeups.add((org, root))
            return False
        self._runs[(org, root)] = asyncio.create_task(
            self._drive(org, root, kind, persist=persist), name="workflow:" + root
        )
        return True

    async def _execute(self, org: str, root: str, kind: WorkflowKind) -> None:
        if kind is WorkflowKind.BUSINESS:
            from ai_orchestrator.application.business_workflow import BusinessWorkflowService

            await BusinessWorkflowService(self.db, org).run(root)
        else:
            from ai_orchestrator.application.agent_blueprints import AgentBlueprintService

            async with self.db.committing_tenant_session(org) as session:
                await AgentBlueprintService(session, org, self.db).run(root)
                await session.commit()

    async def _drive(self, org: str, root: str, kind: WorkflowKind, *, persist: bool) -> None:
        try:
            if self._durable and persist:
                async with self.db.tenant_session(org) as session:
                    await enqueue(session, org, root, kind)
            while True:
                self._wakeups.discard((org, root))
                async with self._slots:
                    await self._run_durable(
                        org, root, kind
                    ) if self._durable else await self._handler(org, root, kind)
                if self._closing or (org, root) not in self._wakeups:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            # The service records the persistent outcome. Keep unexpected faults
            # observable and retrieve every background exception here.
            logger.exception(
                "workflow.driver_failed", organization_id=org, root_id=root, kind=kind.value
            )
        finally:
            self._runs.pop((org, root), None)
            self._wakeups.discard((org, root))

    async def submit(
        self, org: str, root: str, kind: WorkflowKind, *, session: AsyncSession | None = None
    ) -> bool:
        # The response is sent only after the request has a durable generation.
        if session is not None:
            await enqueue(session, org, root, kind)
            await session.commit()
        else:
            async with self.db.tenant_session(org) as owned:
                await enqueue(owned, org, root, kind)
        return self.start(org, root, kind, persist=False)

    def start_monitor(self) -> None:
        settings = get_settings()
        if settings.environment is Environment.TEST or settings.model_provider_default in {
            "fake",
            "scripted",
            "deterministic",
        }:
            return
        if self._monitor is None and not self._closing:
            self._monitor = asyncio.create_task(self._watch(), name="durable-workflow-dispatch")

    async def scan(self, *, organizations: Sequence[str] | None = None) -> None:
        if organizations is None:
            async with self.db.session_factory() as session:
                orgs = (
                    await session.scalars(
                        select(Organization.id).where(Organization.status == "active")
                    )
                ).all()
        else:
            orgs = organizations
        for org in orgs:
            async with self.db.tenant_session(org) as session:
                rows = (
                    await session.execute(
                        select(WorkflowCommand.root_task_id, WorkflowCommand.kind)
                        .where(
                            WorkflowCommand.organization_id == org,
                            WorkflowCommand.requested_seq > WorkflowCommand.settled_seq,
                        )
                        .order_by(WorkflowCommand.updated_at)
                        .limit(50)
                    )
                ).all()
            for root, kind in rows:
                if not self.is_running(org, root):
                    self.start(org, root, WorkflowKind(kind), persist=False)

    async def _watch(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("workflow.dispatch_scan_failed")
            await asyncio.sleep(get_settings().workflow_dispatch_poll_s)

    async def _run_durable(self, org: str, root: str, kind: WorkflowKind) -> None:
        async with self.db.engine.begin() as lock:
            acquired = await lock.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
                {"org": org, "key": "dispatch:" + root},
            )
            if not acquired:
                return  # Another process owns the generation; it alone may acknowledge.
            while not self._closing:
                async with self.db.tenant_session(org) as session:
                    command = await command_for(session, org, root, lock=True)
                    if not command or command.requested_seq <= command.settled_seq:
                        return
                    sequence, paused = command.requested_seq, command.paused
                    command.state = "running"
                try:
                    if paused:
                        from ai_orchestrator.application.workflow_feedback import (
                            WorkflowFeedbackService,
                        )

                        await WorkflowFeedbackService(self.db, org).assess(root)
                    else:
                        job = asyncio.ensure_future(self._handler(org, root, kind))
                        try:
                            while not job.done():
                                done, _ = await asyncio.wait(
                                    {job}, timeout=get_settings().workflow_dispatch_poll_s
                                )
                                if done:
                                    break
                                async with self.db.tenant_session(org) as session:
                                    current = await command_for(session, org, root)
                                    task = await session.get(Task, root)
                                    stop = bool(current and current.paused) or bool(
                                        task and task.status in {"canceled", "expired"}
                                    )
                                if stop:
                                    job.cancel()
                                    await asyncio.gather(job, return_exceptions=True)
                                    break
                            if not job.cancelled():
                                await job
                        finally:
                            if not job.done():
                                job.cancel()
                                await asyncio.gather(job, return_exceptions=True)
                    async with self.db.tenant_session(org) as session:
                        command = await command_for(session, org, root, lock=True)
                        assert command is not None
                        command.settled_seq = sequence
                        command.state = "paused" if command.paused else "waiting"
                except asyncio.CancelledError:
                    # Leave the generation pending; restart reclaims it from PostgreSQL.
                    async with self.db.tenant_session(org) as session:
                        command = await command_for(session, org, root, lock=True)
                        if command:
                            command.state = "suspended"
                    raise
                except Exception as exc:
                    async with self.db.tenant_session(org) as session:
                        command = await command_for(session, org, root, lock=True)
                        if command:
                            command.settled_seq = sequence
                            command.state = "dispatch_failed"
                            command.last_error = type(exc).__name__ + ": " + str(exc)[:1000]
                    raise

    async def cancel(self, org: str, root: str) -> None:
        task = self._runs.get((org, root))
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def wait(self, org: str, root: str) -> None:
        task = self._runs.get((org, root))
        if task:
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        self._closing = True
        if self._monitor:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
            self._monitor = None
        tasks = list(self._runs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
