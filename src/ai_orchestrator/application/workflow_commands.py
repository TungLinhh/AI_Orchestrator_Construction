"""Durable native requests. One generation per root, independent of controller writes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import WorkflowCommand


async def command_for(
    session: AsyncSession, org: str, root: str, *, lock: bool = False
) -> WorkflowCommand | None:
    query = select(WorkflowCommand).where(
        WorkflowCommand.organization_id == org, WorkflowCommand.root_task_id == root
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query.execution_options(populate_existing=True))


async def enqueue(
    session: AsyncSession, org: str, root: str, kind: WorkflowKind
) -> WorkflowCommand:
    await session.execute(
        insert(WorkflowCommand)
        .values(
            id=make_id("wfc"),
            organization_id=org,
            root_task_id=root,
            kind=kind.value,
            requested_seq=1,
        )
        .on_conflict_do_update(
            constraint="uq_workflow_commands_org_root",
            set_={
                "requested_seq": WorkflowCommand.requested_seq + 1,
                "state": "pending",
                "last_error": None,
                "updated_at": utcnow(),
            },
        )
    )
    command = await command_for(session, org, root, lock=True)
    assert command is not None
    return command


def command_view(command: WorkflowCommand | None) -> dict[str, Any]:
    if command is None:
        return {"state": "not_requested", "paused": False, "feedback": {}}
    return {
        "state": command.state,
        "paused": command.paused,
        "requested_seq": command.requested_seq,
        "settled_seq": command.settled_seq,
        "last_error": command.last_error,
        "feedback": command.feedback,
    }
