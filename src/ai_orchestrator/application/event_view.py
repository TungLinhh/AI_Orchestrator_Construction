"""Names on events, resolved for a reader.

## Why this module exists, and where it came from

The event log stores ids — `source_agent_id`, `parent_task_id` — and no names. That is a
deliberate schema decision: the log is the durable record in the domain's own terms, and
presentation concerns do not belong in it.

But a person cannot read ids. So the *projection* resolves them, and there were **two**
projections that did not agree: the SSE stream resolved `view.from_agent`, `view.to_agent`
and `view.title` (so a live feed read "Procurement Agent asked the Executive Agent for
\"run the tender\""), while the task report returned the raw rows (so opening the same task
rendered `task.delegation_accepted · agt_01m3…` — a type name and an opaque id).

**One log, two readings, and the refresh disagreed with the live view.** The reader's
complaint was "I cannot see anything the agents are doing", and they were right twice
over: the feed was never drawn, and the log that *was* drawn was unreadable.

So the enrichment lives here, once, and both callers — the stream and the task report —
use it. Two serialisations of "an event" that drift apart is how a live view and a page
refresh start disagreeing, and this module is the thing that stops them.
"""

from __future__ import annotations

from typing import Any

#: Event types whose frames get enriched, and the task id each one is *about* — which
#: is not always the subject. A delegation's subject is the delegation, not the task.
_ENRICHED = {
    "task.created": "task_id",
    "task.assigned": "task_id",
    "task.started": "task_id",
    "task.updated": "task_id",
    "task.completed": "task_id",
    "task.failed": "task_id",
    "task.canceled": "task_id",
    "delegation.created": "parent_task_id",
    "delegation.accepted": "parent_task_id",
    "delegation.rejected": "parent_task_id",
    "delegation.blocked": "parent_task_id",
    "delegation.completed": "parent_task_id",
    "subagent.spawned": "parent_task_id",
    "subagent.completed": "parent_task_id",
    "subagent.failed": "parent_task_id",
    "workflow.started": "task_id",
    "workflow.completed": "task_id",
    "workflow.failed": "task_id",
    "approval.requested": "task_id",
    "approval.decided": "task_id",
}


async def enrich(frames: list[dict[str, Any]], session: Any, organization_id: str) -> None:
    """Add the names and ids a person needs, in place.

    **The events do not carry them, and that is a deliberate schema decision, so the
    joining happens here.** A `delegation.accepted` event names
    `parent_task_id`, `source_agent_id`, `target_agent_id` and `depth` — no title, no
    objective, no child task. A view built from that alone shows two opaque ids per
    delegation and no way to tell a delegation to Finance from one to Legal.

    So the *projection* resolves them, which is the right layer for it: the event log
    stays the record of what happened in terms the domain chose, and the browser gets
    what a person can read. Widening the event payload instead would put presentation
    concerns into the durable schema, and every existing consumer of `delegation.*`
    would then depend on fields added for a UI.

    Unresolvable references are left as the raw id rather than dropped. A card labelled
    with an id is honest about not knowing; a card with a blank field looks like the
    platform has nothing to say.
    """
    wanted_tasks: set[str] = set()
    wanted_agents: set[str] = set()
    for frame in frames:
        data = frame.get("data")
        if not isinstance(data, dict):
            continue
        key = _ENRICHED.get(frame.get("type", ""))
        if key and data.get(key):
            wanted_tasks.add(str(data[key]))
        for field in ("parent_task_id", "child_task_id", "task_id"):
            if data.get(field):
                wanted_tasks.add(str(data[field]))
        for field in ("source_agent_id", "target_agent_id", "agent_id", "owner_agent_id"):
            if data.get(field):
                wanted_agents.add(str(data[field]))

    titles = await _titles(session, organization_id, wanted_tasks)
    agents = await _agent_names(session, organization_id, wanted_agents)

    for frame in frames:
        data = frame.get("data")
        if not isinstance(data, dict):
            continue
        key = _ENRICHED.get(frame.get("type", ""))
        view: dict[str, Any] = {}
        if key and data.get(key):
            task_id = str(data[key])
            view["task_id"] = task_id
            view["title"] = titles.get(task_id, task_id)
        if data.get("parent_task_id"):
            view["parent_title"] = titles.get(str(data["parent_task_id"]))
        if data.get("child_task_id"):
            child = str(data["child_task_id"])
            view["child_task_id"] = child
            view["child_title"] = titles.get(child, child)
        for field, key_out in (
            ("source_agent_id", "from_agent"),
            ("target_agent_id", "to_agent"),
            ("agent_id", "agent"),
            ("owner_agent_id", "owner_agent"),
        ):
            if data.get(field):
                view[key_out] = agents.get(str(data[field]), str(data[field]))
        if data.get("depth") is not None:
            view["depth"] = data["depth"]
        for field in ("objective", "reason", "status", "task_type", "goal"):
            if data.get(field):
                view[field] = data[field]
        if view:
            frame["view"] = view


async def _titles(session: Any, organization_id: str, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    from sqlalchemy import select as _select

    from ai_orchestrator.persistence.models import Task as _Task

    rows = (
        await session.execute(
            _select(_Task.id, _Task.title, _Task.status).where(
                _Task.organization_id == organization_id, _Task.id.in_(list(ids))
            )
        )
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        out[str(row.id)] = str(row.title or row.id)
        if row.status:
            out.setdefault(f"{row.id}\x00status", str(row.status))
    return out


async def _agent_names(session: Any, organization_id: str, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    from sqlalchemy import select as _select

    from ai_orchestrator.persistence.models import Agent as _Agent

    rows = (
        await session.execute(
            _select(_Agent.id, _Agent.name).where(
                _Agent.organization_id == organization_id, _Agent.id.in_(list(ids))
            )
        )
    ).all()
    return {str(row.id): str(row.name) for row in rows}


__all__ = ["enrich"]
