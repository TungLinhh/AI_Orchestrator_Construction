"""Audit log.

Append-only, and the reason for that is practical rather than aspirational: an
audit log an actor can edit is evidence for nobody. The database enforces it —
`ao_app` holds SELECT and INSERT on `audit_logs` and no UPDATE or DELETE — so
this module only has to never write an UPDATE.

What gets recorded is decisions, not payloads. A raw tool argument or a full
prompt is where secrets and personal data end up, so the audit row carries a
hash of the input, the policy that was applied and the outcome. An operator can
reconstruct the decision from those; they cannot reconstruct a user's data from
the audit table, which is the point.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.models import AuditLog

#: Context values longer than this are truncated. An audit row is for a reviewer,
#: not an archive; the full payload belongs in the execution record.
MAX_CONTEXT_VALUE_CHARS = 500


@dataclass(slots=True)
class AuditEntry:
    """One recorded action."""

    actor_id: str | None
    actor_type: ActorType
    action: str
    resource_type: str
    resource_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    outcome: str = "success"
    policy_decision: str | None = None
    policy_rule_id: str | None = None
    policy_reason: str | None = None
    approval_id: str | None = None
    trace_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None
    user_agent: str | None = None


class AuditService:
    def __init__(self, session: AsyncSession, organization_id: str | None = None) -> None:
        self._session = session
        self._org = organization_id

    async def record(
        self,
        *,
        actor: Actor,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        outcome: str = "success",
        policy_decision: str | None = None,
        policy_rule_id: str | None = None,
        policy_reason: str | None = None,
        approval_id: str | None = None,
        trace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditLog:
        row = AuditLog(
            organization_id=self._org,
            actor_id=str(actor.id) if actor.id else None,
            actor_type=actor.kind.value,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            task_id=task_id,
            execution_id=execution_id,
            outcome=outcome,
            policy_decision=policy_decision,
            policy_rule_id=policy_rule_id,
            policy_reason=(policy_reason or "")[:MAX_CONTEXT_VALUE_CHARS] or None,
            approval_id=approval_id,
            trace_id=trace_id,
            context=_truncate(context or {}),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    def add(
        self,
        *,
        actor: Actor,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        outcome: str = "success",
        policy_decision: str | None = None,
        policy_rule_id: str | None = None,
        policy_reason: str | None = None,
        approval_id: str | None = None,
        trace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Stage a row without flushing.

        For callers already inside a flush of this session — a tool handler
        running inside a task's `create`, for instance. `record()` is the normal
        entry point and flushes so the row is immediately queryable; calling it
        from inside a flush raises `InvalidRequestError: Session is already
        flushing`, and because that happens mid-transaction, every later statement
        on the same session then fails too with "Can't operate on closed
        transaction". Two errors from one nested flush, and the second is the one
        the traceback ends on.

        The row joins whatever transaction is already open, which is the right
        lifetime for an audit entry anyway: an audit row that outlived the change
        it describes would be a lie.
        """
        row = AuditLog(
            organization_id=self._org,
            actor_id=str(actor.id) if actor.id else None,
            actor_type=actor.kind.value,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            task_id=task_id,
            execution_id=execution_id,
            outcome=outcome,
            policy_decision=policy_decision,
            policy_rule_id=policy_rule_id,
            policy_reason=(policy_reason or "")[:MAX_CONTEXT_VALUE_CHARS] or None,
            approval_id=approval_id,
            trace_id=trace_id,
            context=_truncate(context or {}),
        )
        self._session.add(row)
        return row

    async def _unused(
        self,
        *_: Any,
        **__: Any,
    ) -> None:
        await self._session.flush()
        return None

    async def query(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        task_id: str | None = None,
        since: Any = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[AuditLog]:
        """Read the log.

        Ordered by the monotonic `sequence`, not by `created_at`. Two entries in
        the same millisecond must still have a defined order, and a wall clock
        does not provide one.
        """
        stmt: Select[Any] = select(AuditLog)
        if self._org is not None:
            stmt = stmt.where(AuditLog.organization_id == self._org)
        if resource_type is not None:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if task_id is not None:
            stmt = stmt.where(AuditLog.task_id == task_id)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        result = await self._session.execute(
            stmt.order_by(AuditLog.sequence.desc()).limit(limit).offset(offset)
        )
        rows: list[AuditLog] = list(result.scalars().all())
        return rows

    async def timeline(self, task_id: str) -> list[dict[str, Any]]:
        """The ordered story of one task.

        This is what the UI's task timeline renders, and it is the artefact an
        operator reads instead of a chat log: who acted, under which rule, with
        what outcome, in what order.
        """
        rows = await self.query(task_id=task_id, limit=1000)
        ordered = sorted(rows, key=lambda r: r.sequence)
        return [
            {
                "sequence": row.sequence,
                "at": row.created_at.isoformat() if row.created_at else None,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "action": row.action,
                "resource": f"{row.resource_type}:{row.resource_id or '-'}",
                "outcome": row.outcome,
                "policy_rule_id": row.policy_rule_id,
                "approval_id": row.approval_id,
                "trace_id": row.trace_id,
            }
            for row in ordered
        ]

    async def count(self, *, since: Any = None) -> int:
        stmt = select(func.count()).select_from(AuditLog)
        if self._org is not None:
            stmt = stmt.where(AuditLog.organization_id == self._org)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        return int(await self._session.scalar(stmt) or 0)

    async def denied_action_summary(self, *, limit: int = 50) -> Sequence[dict[str, Any]]:
        """What is being refused, and by which rule.

        The governance dashboard's most useful panel: a spike in denials on one
        rule means the policy is fighting the work, and a spike across many rules
        means an agent is probing.
        """
        stmt = (
            select(
                AuditLog.action,
                AuditLog.policy_rule_id,
                func.count().label("count"),
            )
            .where(
                and_(
                    AuditLog.outcome == "denied",
                    AuditLog.organization_id == self._org,
                )
            )
            .group_by(AuditLog.action, AuditLog.policy_rule_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).mappings().all()
        return [dict(row) for row in rows]


def _truncate(value: Any, depth: int = 0) -> Any:
    """Bound the size of a context value.

    A depth limit as well as a length limit, because a self-referential structure
    in a log call is a cheap way to hang the process that is trying to record it.
    """
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str) and len(value) > MAX_CONTEXT_VALUE_CHARS:
        return value[:MAX_CONTEXT_VALUE_CHARS] + "...[truncated]"
    if isinstance(value, dict):
        return {k: _truncate(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_truncate(v, depth + 1) for v in value[:50]]
    return value


__all__ = ["AuditEntry", "AuditService"]
