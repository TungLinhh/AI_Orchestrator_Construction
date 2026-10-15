"""Reconcile obsolete waiting reviews without approving or fabricating human decisions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Approval, Execution, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository


async def reconcile_stale_reviews(
    session: AsyncSession, org: str, actor: Actor, *, apply: bool = False
) -> list[dict[str, Any]]:
    repo = TaskRepository(session, org)
    candidates = list(
        await session.scalars(
            select(Task.id)
            .where(Task.organization_id == org, Task.status == "waiting_for_approval")
            .order_by(Task.id)
        )
    )
    findings = []
    for id in candidates:
        blocker = await repo.terminal_lineage(id)
        if not blocker:
            continue
        running = await session.scalar(
            select(Execution.id)
            .where(
                Execution.organization_id == org,
                Execution.task_id == id,
                Execution.status == "running",
            )
            .limit(1)
        )
        finding = {
            "task_id": id,
            "blocker": blocker,
            "running_execution": running,
            "applied": False,
        }
        findings.append(finding)
        if not apply or running:
            continue
        approvals = list(
            await session.scalars(
                select(Approval)
                .where(
                    Approval.organization_id == org,
                    Approval.task_id == id,
                    Approval.status == "pending",
                )
                .order_by(Approval.id)
                .with_for_update()
            )
        )
        task = await session.scalar(
            select(Task)
            .where(Task.organization_id == org, Task.id == id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None or task.status != "waiting_for_approval":
            continue
        for approval in approvals:
            approval.status = "expired"
            approval.decision_note = "Task lineage ended; obsolete review, no human decision made."
            approval.updated_at = utcnow()
        await repo.transition(id, Transition.CANCEL)
        await AuditService(session, org).record(
            actor=actor,
            action="task.obsolete_review_reconciled",
            resource_type="task",
            resource_id=id,
            task_id=id,
            context={"blocker": blocker, "expired_approvals": [a.id for a in approvals]},
        )
        finding["applied"] = True
    return findings
