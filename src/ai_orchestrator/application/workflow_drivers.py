"""Process-local dispatch and shutdown; persistent lifecycle lives in workflow_lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
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
    ) -> None:
        self.db = db
        self._handler = handler or self._execute
        self._runs: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._wakeups: set[tuple[str, str]] = set()
        self._closing = False
        self._slots = asyncio.Semaphore(concurrency or get_settings().native_workflow_concurrency)

    def is_running(self, org: str, root: str) -> bool:
        task = self._runs.get((org, root))
        return task is not None and not task.done()

    def start(self, org: str, root: str, kind: WorkflowKind) -> bool:
        if self._closing:
            return False
        if self.is_running(org, root):
            # A human may approve just after a gate commits but before its
            # driver exits. Coalesce that wakeup instead of silently losing it.
            self._wakeups.add((org, root))
            return False
        self._runs[(org, root)] = asyncio.create_task(
            self._drive(org, root, kind), name="workflow:" + root
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

    async def _drive(self, org: str, root: str, kind: WorkflowKind) -> None:
        try:
            while True:
                self._wakeups.discard((org, root))
                async with self._slots:
                    await self._handler(org, root, kind)
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
        tasks = list(self._runs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
