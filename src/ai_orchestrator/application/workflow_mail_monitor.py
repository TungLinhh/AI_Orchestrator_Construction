"""Read-only CV polling for committed, waiting hiring campaigns."""

import asyncio

from sqlalchemy import text

from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
from ai_orchestrator.config.settings import Environment, get_settings
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)


async def pending_mail_intakes(db: Database, org: str) -> list[str]:
    async with db.tenant_session(org) as session:
        rows = await session.execute(
            text(
                "SELECT DISTINCT root.id FROM tasks root JOIN tasks child "
                "ON child.parent_task_id=root.id AND child.organization_id=root.organization_id "
                "WHERE root.organization_id=:org AND root.status='blocked' "
                "AND root.input->>'mode'='live' "
                "AND root.input->>'business_workflow'='mep_hiring' "
                "AND child.input->>'stage_key'='cv_intake' "
                "AND child.status='waiting_for_input' ORDER BY root.id LIMIT 10"
            ),
            {"org": org},
        )
        return [str(row[0]) for row in rows]


class WorkflowMailMonitor:
    def __init__(self, drivers: WorkflowDrivers) -> None:
        self.drivers = drivers
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        settings = get_settings()
        if (
            self._task is not None
            or settings.environment == Environment.TEST
            or settings.model_provider_default in {"fake", "scripted", "deterministic"}
            or not settings.recruitment_mail_org
        ):
            return
        self._task = asyncio.create_task(self._watch(), name="workflow-mail-intake-monitor")

    async def _watch(self) -> None:
        settings = get_settings()
        while True:
            try:
                for root in await pending_mail_intakes(
                    self.drivers.db, settings.recruitment_mail_org
                ):
                    self.drivers.start(settings.recruitment_mail_org, root, WorkflowKind.BUSINESS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("workflow.mail_monitor_failed")
            await asyncio.sleep(settings.recruitment_mail_poll_interval_s)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
