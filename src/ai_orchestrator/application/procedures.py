"""Reading a run's shape, and counting how often it has happened.

This is `SELF_IMPROVEMENT.md` §9 steps 1 and 3. Step 1 is the fingerprint, which
lives in `domain.procedure`; this module is the part that needs a database: turning
a finished run's audit trace into that fingerprint, storing it, and answering
"has this agent done this before?".

The repetition count is the gate the whole self-improvement design waits on. Until
something counts, the gate can never open, and a design whose precondition has never
been met is a design, not a system. This is deliberately the smallest thing that can
open it: no proposals, no approvals, no lessons — just an honest count.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.ids import TaskId
from ai_orchestrator.domain.procedure import describe, procedure_fingerprint, same_procedure
from ai_orchestrator.persistence.models import AuditLog, Task

#: The audit action a tool call writes. Named here so the reader and the writer
#: cannot drift: a silent rename here would make every count read zero, and zero
#: counts look exactly like "nothing has repeated".
TOOL_INVOKE_ACTION = "tool.invoke"


class ProcedureReader:
    """Turns traces into fingerprints, and counts repetitions."""

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    @property
    def organization_id(self) -> str:
        """The tenant this reader is bound to.

        A property rather than a public attribute because the review job needs it
        and `self._org` from another module is a reach-through. The binding is
        fixed at construction and must not be reassignable, so this is read-only by
        design and not by convention.
        """
        return self._org

    async def fingerprint_for(self, task_id: str) -> str | None:
        """The fingerprint of one run, from the trace it left.

        `None` when the run wrote no trace, which is a real state — a task that ran
        before traces existed — and is reported as `None` rather than as a
        fingerprint of nothing, because "this shape of work, with no steps" is a
        claim and an unobserved run is not one.
        """
        rows = await self._trace_rows(task_id)
        if not rows:
            return None
        task = (
            await self._session.execute(
                sa.select(Task.task_type).where(
                    Task.id == task_id, Task.organization_id == self._org
                )
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        return procedure_fingerprint(task_type=str(task), tool_calls=rows)

    async def record_for(self, task_id: str) -> str | None:
        """Compute a run's fingerprint and store it on the task.

        Idempotent: a run's trace does not change once it has finished, so calling
        this twice writes the same value. Written without a flush, because this is
        called from the middle of a task's own transaction and a nested flush is a
        hard error (see `AuditService.add`).
        """
        value = await self.fingerprint_for(task_id)
        if value is None:
            return None
        task = (
            await self._session.execute(
                sa.select(Task).where(Task.id == task_id, Task.organization_id == self._org)
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        task.procedure_fingerprint = value
        return value

    async def repetition_count(
        self, *, agent_id: str, fingerprint: str, exclude_task_id: str | None = None
    ) -> int:
        """How many times this agent has done this shape of work.

        `exclude_task_id` excludes the run being recorded, so that recording a
        procedure does not itself count as a repetition of it. Without that, every
        count would be one higher than it should be and the gate would open a
        repetition early.

        Null fingerprints are never counted. "Not observed" is not an observation,
        and grouping the unobserved together would invent a procedure out of the
        absence of one.
        """
        if not fingerprint or not agent_id:
            return 0
        stmt = (
            sa.select(sa.func.count())
            .select_from(Task)
            .where(
                Task.organization_id == self._org,
                Task.owner_agent_id == agent_id,
                Task.procedure_fingerprint == fingerprint,
            )
        )
        if exclude_task_id:
            stmt = stmt.where(Task.id != exclude_task_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def seen_procedures(self, *, agent_id: str, limit: int = 50) -> list[tuple[str, int]]:
        """The shapes this agent has done, most-repeated first.

        For a human looking at an agent's history. Returns `(fingerprint, count)`
        pairs; the fingerprint alone is not readable, which is the honest limit of
        what this table can show without storing the trace alongside it.
        """
        stmt = (
            sa.select(Task.procedure_fingerprint, sa.func.count().label("n"))
            .where(
                Task.organization_id == self._org,
                Task.owner_agent_id == agent_id,
                Task.procedure_fingerprint.isnot(None),
            )
            .group_by(Task.procedure_fingerprint)
            .order_by(sa.desc("n"))
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(str(f), int(n)) for f, n in rows if f]

    async def has_repeated(
        self, *, agent_id: str, fingerprint: str, threshold: int, exclude_task_id: str | None = None
    ) -> tuple[bool, int]:
        """Whether the repetition gate opens for this shape, and at what count.

        The gate Hermes uses is a threshold, and this is it: a change proposed from
        one observation is a change proposed from a coincidence. The count is
        returned as well as the verdict, because "repeated 4 times" is the evidence
        and `True` is not.
        """
        count = await self.repetition_count(
            agent_id=agent_id, fingerprint=fingerprint, exclude_task_id=exclude_task_id
        )
        return count >= threshold, count

    async def _trace_rows(self, task_id: str) -> list[dict[str, object]]:
        """The ordered `tool.invoke` rows for one task.

        Ordered by the audit sequence, which is the order the calls were made. The
        ordering is the whole point of a procedure, so it is not left to the
        planner.
        """
        result = await self._session.execute(
            sa.select(
                AuditLog.sequence,
                AuditLog.resource_id,
                AuditLog.outcome,
                AuditLog.context,
            )
            .where(
                AuditLog.organization_id == self._org,
                AuditLog.task_id == task_id,
                AuditLog.action == TOOL_INVOKE_ACTION,
            )
            .order_by(AuditLog.sequence)
        )
        return [
            {
                "sequence": row.sequence,
                "tool": row.resource_id,
                "outcome": row.outcome,
                "arguments": (row.context or {}).get("arguments"),
            }
            for row in result
        ]


def label(fingerprint: str) -> str:
    """A short label for logs and approval packets. Re-exported so callers that
    already hold a `ProcedureReader` do not have to import two modules."""
    return describe(fingerprint)


__all__ = ["TOOL_INVOKE_ACTION", "ProcedureReader", "TaskId", "label", "same_procedure"]
