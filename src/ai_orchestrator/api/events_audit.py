"""Event, audit and governance endpoints.

Read-only projections. Both tables are append-only in the database, so there is
no write path here by design — an operator inspecting the platform cannot alter
what they are inspecting.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.audit import AuditService
from ai_orchestrator.persistence.models import AuditLog, Event, OutboxEvent

router = APIRouter(tags=["events", "audit"])


def _event_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "schema_version": event.schema_version,
        "source": event.source,
        "subject": event.subject,
        "spec_version": event.spec_version,
        "trace_id": event.trace_id,
        "actor_id": event.actor_id,
        "data": event.data,
        "occurred_at": event.occurred_at.isoformat(),
    }


@router.get("/events")
async def list_events(
    type_filter: str | None = Query(default=None, alias="type"),
    subject: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The event log, newest first.

    Filtered by type and subject. `type` carries its version, so a consumer can
    pin the version it understands.
    """
    stmt = select(Event).where(Event.organization_id == ctx.organization_id)
    if type_filter:
        stmt = stmt.where(Event.type == type_filter)
    if subject:
        stmt = stmt.where(Event.subject == subject)
    rows = (
        (
            await ctx.session.execute(
                stmt.order_by(Event.occurred_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int(
        (await ctx.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )
    return {
        "items": [_event_dict(e) for e in rows],
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
        "total": total,
    }


@router.get("/events/stats")
async def event_stats(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Event volume by type, and outbox health.

    The outbox numbers are the ones that matter operationally: a growing pending
    count means state is changing and nothing downstream is hearing about it.
    """
    by_type = (
        await ctx.session.execute(
            select(Event.type, func.count())
            .where(Event.organization_id == ctx.organization_id)
            .group_by(Event.type)
            .order_by(func.count().desc())
        )
    ).all()
    outbox = (
        await ctx.session.execute(
            select(
                func.count().filter(OutboxEvent.published_at.is_(None)),
                func.count().filter(OutboxEvent.dead_lettered_at.is_not(None)),
                func.count(),
            ).where(OutboxEvent.organization_id == ctx.organization_id)
        )
    ).one()
    return {
        "by_type": {t: c for t, c in by_type},
        "outbox": {
            "pending": int(outbox[0] or 0),
            "quarantined": int(outbox[1] or 0),
            "total": int(outbox[2] or 0),
        },
    }


@router.get("/events/{event_id}")
async def get_event(event_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    from ai_orchestrator.domain.errors import NotFoundError

    event = (
        await ctx.session.execute(
            select(Event).where(Event.id == event_id, Event.organization_id == ctx.organization_id)
        )
    ).scalar_one_or_none()
    if event is None:
        msg = f"event not found: {event_id}"
        raise NotFoundError(msg, resource_type="event", resource_id=event_id)
    return _event_dict(event)


@router.get("/audit")
async def list_audit(
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    task_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The audit log, newest first, ordered by the monotonic sequence."""
    service = AuditService(ctx.session, ctx.organization_id)
    rows = await service.query(
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=actor_id,
        action=action,
        task_id=task_id,
        limit=limit,
        offset=offset,
    )
    items = [
        {
            "sequence": r.sequence,
            "at": r.created_at.isoformat(),
            "actor_type": r.actor_type,
            "actor_id": r.actor_id,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "task_id": r.task_id,
            "outcome": r.outcome,
            "policy_decision": r.policy_decision,
            "policy_rule_id": r.policy_rule_id,
            "approval_id": r.approval_id,
            "trace_id": r.trace_id,
            "context": r.context,
        }
        for r in rows
    ]
    stmt = (
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.organization_id == ctx.organization_id)
    )
    for column, value in (
        (AuditLog.resource_type, resource_type),
        (AuditLog.resource_id, resource_id),
        (AuditLog.actor_id, actor_id),
        (AuditLog.action, action),
        (AuditLog.task_id, task_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    total = int((await ctx.session.execute(stmt)).scalar_one())
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": len(items),
    }


@router.get("/audit/denials")
async def denial_summary(
    limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """What is being refused, and by which rule.

    A spike on one rule means the policy is fighting the work. A spike across
    many rules means an agent is probing. The two need different responses, and
    they look the same in a total count.
    """
    service = AuditService(ctx.session, ctx.organization_id)
    rows = await service.denied_action_summary(limit=limit)
    return {"items": rows, "count": len(rows)}


@router.get("/governance/summary")
async def governance_summary(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The governance dashboard's single view.

    Deliberately generic: no department KPIs, because those are an operator's
    business definition and not the platform's. What the platform can answer is
    about authority, permissions and spend.
    """
    from sqlalchemy import text as sql

    elevated_agents: int = (
        await ctx.session.execute(
            sql(
                "SELECT count(*) FROM agents WHERE organization_id = :org "
                "AND autonomy_level IN ('l3_human_approval', 'l4_bounded_autonomous') "
                "AND lifecycle_status = 'active'"
            ),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    privileged_bindings: int = (
        await ctx.session.execute(
            sql(
                "SELECT count(*) FROM agent_tool_bindings b "
                "JOIN tools t ON t.id = b.tool_id "
                "WHERE b.organization_id = :org AND b.is_enabled "
                "AND t.risk_level IN ('privileged', 'destructive')"
            ),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    pending_approvals: int = (
        await ctx.session.execute(
            sql(
                "SELECT count(*) FROM approvals WHERE organization_id = :org AND status = 'pending'"
            ),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    blocked_tasks: int = (
        await ctx.session.execute(
            sql("SELECT count(*) FROM tasks WHERE organization_id = :org AND status = 'blocked'"),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    waiting_approvals: int = (
        await ctx.session.execute(
            sql(
                "SELECT count(*) FROM tasks WHERE organization_id = :org "
                "AND status = 'waiting_for_approval'"
            ),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    total_cost: Decimal = (
        await ctx.session.execute(
            sql("SELECT COALESCE(sum(cost_usd), 0) FROM executions WHERE organization_id = :org"),
            {"org": ctx.organization_id},
        )
    ).scalar_one()
    total_tokens: int = (
        await ctx.session.execute(
            sql(
                "SELECT COALESCE(sum(input_tokens + output_tokens + reasoning_tokens), 0) "
                "FROM executions WHERE organization_id = :org"
            ),
            {"org": ctx.organization_id},
        )
    ).scalar_one()

    return {
        "agents_with_elevated_autonomy": int(elevated_agents),
        "privileged_tool_bindings": int(privileged_bindings),
        "pending_approvals": int(pending_approvals),
        "tasks_waiting_for_approval": int(waiting_approvals),
        "blocked_tasks": int(blocked_tasks),
        "total_cost_usd": float(total_cost),
        "total_tokens": int(total_tokens),
    }


@router.get("/governance/spend")
async def spend_by_agent(
    limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Cost and tokens per agent.

    Sorted by cost. The agent with the highest spend is usually the one with a
    problem — an agent loop, a profile routed to an expensive model, or a
    delegation cycle that is not quite a cycle.
    """
    from sqlalchemy import text as sql

    rows = (
        (
            await ctx.session.execute(
                sql(
                    """
                SELECT a.id AS agent_id, a.name,
                       COALESCE(sum(e.cost_usd), 0) AS cost_usd,
                       COALESCE(sum(e.input_tokens + e.output_tokens + e.reasoning_tokens), 0)
                           AS tokens,
                       count(e.id) AS executions,
                       count(*) FILTER (WHERE e.status = 'failed') AS failures
                  FROM agents a
                  LEFT JOIN executions e ON e.agent_id = a.id
                 WHERE a.organization_id = :org
                 GROUP BY a.id, a.name
                 ORDER BY cost_usd DESC
                 LIMIT :limit
                """
                ),
                {"org": ctx.organization_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [
            {
                "agent_id": r["agent_id"],
                "name": r["name"],
                "cost_usd": float(r["cost_usd"]),
                "tokens": int(r["tokens"]),
                "executions": int(r["executions"]),
                "failures": int(r["failures"]),
            }
            for r in rows
        ],
        "count": len(rows),
    }


__all__ = ["router"]
