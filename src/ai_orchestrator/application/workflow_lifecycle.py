"""One lock and recovery owner for native ordered workflow controllers.

The advisory transaction is held on its own connection across stage commits.
Postgres releases it on rollback or process death, including pooled connections.
Tasks and executions are the checkpoints; no second scheduler state is introduced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, TaskStatus
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.state_machines import Transition, is_terminal_task
from ai_orchestrator.domain.workflow_lifecycle import RecoveryAction, WorkflowKind, recovery_action
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Execution, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database

SYSTEM = Actor(id="workflow-lifecycle", kind=ActorType.SYSTEM)


async def _settle(
    db: Database, org: str, root_id: str, kind: WorkflowKind, *, reason: str, failed: bool
) -> list[str]:
    """Settle only unfinished work, preserving usage, receipts and completed rows."""
    async with db.tenant_session(org) as session:
        repo = TaskRepository(session, org)
        root = await repo.get(root_id)
        if root.parent_task_id or not root.input.get(kind.value):
            raise ValidationError("Not a workflow root for this controller")
        rows = list(
            (
                await session.scalars(
                    select(Task).where(Task.organization_id == org, Task.parent_task_id == root_id)
                )
            ).all()
        )
        stopped = root.status in {"canceled", "expired"}
        replay: list[str] = []
        reconcile: list[str] = []
        model_ids = {step["id"] for step in root.input.get("step_ids", [])}
        for task in rows:
            if is_terminal_task(TaskStatus(task.status)):
                continue
            if failed or stopped:
                await repo.transition(task.id, Transition.CANCEL)
            elif task.status == "running" or task.constraints.get("controller_interrupted"):
                action = recovery_action(
                    kind, str(root.input.get(kind.value)), task.input.get("stage_key", "")
                )
                if kind is WorkflowKind.AGENT and task.id not in model_ids:
                    action = RecoveryAction.RECONCILE
                if task.status == "running":
                    await repo.transition(task.id, Transition.BLOCK)
                task.constraints = {**task.constraints, "controller_interrupted": True}
                (replay if action is RecoveryAction.REPLAY else reconcile).append(task.id)
        if not is_terminal_task(TaskStatus(root.status)):
            if failed:
                await repo.transition(
                    root.id, Transition.FAIL, error=reason, failure_category="workflow_controller"
                )
            elif root.status in {"assigned", "running"}:
                await repo.transition(root.id, Transition.BLOCK)
        executions = (
            await session.scalars(
                select(Execution).where(
                    Execution.organization_id == org,
                    Execution.task_id.in_([root_id, *(t.id for t in rows)]),
                    Execution.status == "running",
                )
            )
        ).all()
        for execution in executions:
            # finish() replaces token counts and decision records. Interruption
            # closes the attempt without erasing already committed evidence.
            execution.status = "failed"
            execution.finished_at = utcnow()
            execution.error_category = "workflow_controller" if failed else "controller_interrupted"
            execution.error_message = reason
        root.constraints = {
            **root.constraints,
            "workflow_lifecycle": {
                "state": "stopped"
                if stopped
                else "failed"
                if failed
                else "reconciliation_required"
                if reconcile
                else "suspended",
                "reason": reason,
                "replay_task_ids": sorted(replay),
                "reconciliation_task_ids": sorted(reconcile),
            },
        }
        await AuditService(session, org).record(
            actor=SYSTEM,
            action="workflow.failed" if failed else "workflow.suspended",
            resource_type="task",
            resource_id=root_id,
            task_id=root_id,
            outcome="failure" if failed else "success",
            context={
                "kind": kind.value,
                "reason": reason,
                "replay_task_ids": sorted(replay),
                "reconciliation_task_ids": sorted(reconcile),
                "settled_executions": [e.id for e in executions],
            },
        )
        return reconcile


async def _prepare(db: Database, org: str, root_id: str, kind: WorkflowKind) -> None:
    async with db.tenant_session(org) as session:
        root = await TaskRepository(session, org).get(root_id)
        if root.parent_task_id or not root.input.get(kind.value):
            raise ValidationError("Not a workflow root for this controller")
        if is_terminal_task(TaskStatus(root.status)):
            return
        interrupted = bool(
            root.constraints.get("workflow_lifecycle", {}).get("state")
            in {"suspended", "reconciliation_required"}
        )
        interrupted = interrupted or bool(
            await session.scalar(
                select(Execution.id)
                .where(
                    Execution.organization_id == org,
                    Execution.status == "running",
                    Execution.task_id.in_(
                        select(Task.id).where(
                            Task.organization_id == org,
                            (Task.id == root_id) | (Task.parent_task_id == root_id),
                        )
                    ),
                )
                .limit(1)
            )
        )
        interrupted = interrupted or bool(
            await session.scalar(
                select(Task.id)
                .where(
                    Task.organization_id == org,
                    Task.parent_task_id == root_id,
                    Task.status == "running",
                )
                .limit(1)
            )
        )
    if interrupted:
        reconcile = await _settle(
            db,
            org,
            root_id,
            kind,
            reason="Controller interrupted; recover committed checkpoints",
            failed=False,
        )
        if reconcile:
            raise PreconditionError(
                "Interrupted external action requires evidence reconciliation before resume",
                details={"task_ids": reconcile},
            )
    async with db.tenant_session(org) as session:
        root = await TaskRepository(session, org).get(root_id)
        lifecycle = root.constraints.get("workflow_lifecycle", {})
        for task_id in lifecycle.get("replay_task_ids", []):
            task = await TaskRepository(session, org).get(task_id)
            if task.status == "blocked" and task.constraints.get("controller_interrupted"):
                await TaskRepository(session, org).transition(task_id, Transition.ASSIGN)
                task.constraints = {
                    k: v for k, v in task.constraints.items() if k != "controller_interrupted"
                }
        root.constraints = {
            **root.constraints,
            "workflow_lifecycle": {
                "state": "ready",
                "reason": "",
                "replay_task_ids": [],
                "reconciliation_task_ids": [],
            },
        }


@asynccontextmanager
async def workflow_run(
    db: Database, org: str, root_id: str, kind: WorkflowKind
) -> AsyncIterator[None]:
    async with db.engine.begin() as lock:
        acquired = await lock.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:root))"),
            {"org": org, "root": root_id},
        )
        if not acquired:
            raise PreconditionError("This workflow is already running")
        await _prepare(db, org, root_id, kind)
        try:
            yield
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            await _settle(
                db, org, root_id, kind, reason="Controller stopped before completion", failed=False
            )
            raise
        except Exception as exc:
            await _settle(
                db,
                org,
                root_id,
                kind,
                reason=type(exc).__name__ + ": " + str(exc)[:2000],
                failed=True,
            )
            raise
