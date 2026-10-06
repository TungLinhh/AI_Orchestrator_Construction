"""The CEO's two questions: *what can I give to the agents?* and *what happened to it?*

## Why this is a read layer and not a page

`role_views.py` holds the dossier's three views. This holds the two the coordination flow
adds, and it holds them for the same reason: **the judgements live here and the HTTP layer
adds transport.** A page that computed "is this task waiting on me" from a different query
than the inbox is a page that eventually disagrees with the inbox.

## The work queue, and what makes a task *offered*

Not every task is something a CEO should be choosing from. A task is offered when it is
**open and waiting for a person** — created but never run, or assigned but not finished.
A task that a worker is running is not something to hand out, and offering it invites a
second person to start the same work.

`waiting_on` is the column that decides the sort order, and it is a real classification
rather than a flag:

| `waiting_on` | what it means | what the CEO can do |
|---|---|---|
| `you` | nobody owns it, or an approval is waiting on a person | give it to an agent, or decide |
| `an_agent` | assigned and not finished | watch it |
| `nobody` | finished, failed, or cancelled | read the report |

**`you` is the point of the whole view.** A queue that mixes all three equally gives a CEO
no way to know what needs them, which is the one question a CEO opens a page to answer.

## The report, and why it is not the task row

`GET /tasks/{id}` returns the task. This returns **the account of it**: what was asked,
who it was handed to, what each one did, what was approved and what was refused, what came
out, and who decided at each point. An audit asks those questions together; answering them
one HTTP call at a time is how an auditor assembles a picture of a system that has already
moved on.

The report is assembled from **five tables** and it is explicit about which, because a
report that silently omits a source is a report that can be wrong in a way nobody notices:

* `tasks` — what was asked, and how it ended
* `delegations` — the tree, with each edge's status and any refusal reason
* `executions` — what each agent did, and what it cost
* `approvals` — every decision, with the actor and the reason
* `events` — the ordered record, which is what "the log" means here

## What the report will not do

**It will not say the work was good.** It reports what happened: which agent, which model,
how long, what it produced, and whether a person signed it off. Quality is the approval's
subject, not the report's, and a report that implied a verdict would be doing an
approval's job with a SELECT.

**It will not hide a failed delegation.** A refused edge appears with its reason, in place,
in the tree — because "the agent could not do this and said why" is the most useful line in
the whole account, and a report that only shows successes is a report about a different
system.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ai_orchestrator.application.event_view import enrich
from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.enums import TaskStatus
from ai_orchestrator.domain.errors import NotFoundError
from ai_orchestrator.domain.state_machines import allowed_task_transitions

#: The CEO's queue.
#:
#: `waiting_on` is computed rather than stored, because a stored flag is a thing that can be
#: wrong. The three cases are mutually exclusive and the ordering puts `you` first, which
#: is the only reason to open this page.
_WORK_QUEUE = """
SELECT t.id, t.title, t.goal, t.task_type, t.status, t.priority, t.requester_type,
       t.created_at, t.deadline_at, t.attempt_count, t.failure_category, t.last_error,
       t.root_task_id, t.parent_task_id,
       a.name AS owner_name,
       (SELECT count(*) FROM delegations d
         WHERE d.parent_task_id = t.id) AS delegated,
       (SELECT count(*) FROM tasks c WHERE c.parent_task_id = t.id) AS children,
       (SELECT count(*) FROM executions x WHERE x.task_id = t.id) AS executions,
       (SELECT count(*) FROM approvals ap WHERE ap.task_id = t.id
          AND ap.status = 'pending') AS approvals_waiting,
       {WAITING_ON_CASE} AS waiting_bucket
FROM tasks t
LEFT JOIN agents a ON a.id = t.owner_agent_id
WHERE t.organization_id = CAST(:o AS varchar(40))
  AND (CAST(:state AS text) IS NULL OR t.status = :state)
ORDER BY
  -- `you` first: an approval waiting on a person, or a task nobody owns.
  {WAITING_ON_CASE},
  t.created_at DESC
LIMIT :limit OFFSET :offset
"""

_WORK_COUNT = """
SELECT count(*) FROM tasks t
WHERE t.organization_id = CAST(:o AS varchar(40))
  AND (CAST(:state AS text) IS NULL OR t.status = :state)
"""

#: The account of one task. Each of the five is a separate statement because they have
#: different shapes and forcing them into one row would mean a `json_agg` per source and a
#: reader unable to tell an empty list from a missing one.

#: **The one definition of "waiting on whom", as a CASE over a task row.**
#:
#: It was written twice -- once in Python as `_waiting_on`, once in SQL in this
#: module's `ORDER BY` -- and the counts were computed by looping over the **paginated
#: page** in Python. So the three tiles described one page, not the organisation:
#:
#: ```
#: total      177
#: needs_you    2      <- counted over rows 0..99
#: in_flight   67
#: settled     31      # 2 + 67 + 31 == 100, exactly the limit
#: ```
#:
#: `needs_you + in_flight + settled` summing to the page size is the tell. A person
#: opening the page with `?limit=5` was told the company had five of everything.
#:
#: One constant, substituted into both the ordering and the count, so the row a person
#: filters on and the number that reports how many there are cannot be two different
#: rules. Kept as a SQL string rather than moved into Python because counting the whole
#: table in SQL is one `GROUP BY`, and pulling 177 rows into the process to count them
#: would be the other kind of wrong.
#: `t` must be in scope. Substituted with `.format()`, so every literal brace in the
#: surrounding SQL is doubled -- there is exactly one, and it is this one.
#:
#: **Three buckets, and each names a question a person actually asks.**
#:
#: | bucket | who | why |
#: |---|---|---|
#: | `you` | a person, or nobody yet | an approval pends, or no agent holds it yet |
#: | `an_agent` | an agent, right now | `running` — genuinely in flight |
#: | `nobody` | nobody, any more | terminal: settled, one way or the other |
#:
#: The second bucket used to be "`created` or `assigned`", which belongs to `you`, and the
#: mapping in `ceo_work_queue` then sent it to `an_agent` — so the counts and the rows were
#: two different rules and the register contradicted itself: `needs_you 0` above two rows
#: reading `waiting_on=you`.
#:
#: And the third bucket used to be "everything else", which put a **failed** task in
#: `an_agent`: the page read "With an agent: 2" beside rows that had failed, and a person
#: reading it concludes two agents are working. A failed task has no agent on it — that is
#: the entire meaning of the word. It is settled, badly, but settled.
WAITING_ON_CASE = """\
CASE WHEN (SELECT count(*) FROM approvals ap WHERE ap.task_id = t.id
            AND ap.status = 'pending') > 0 THEN 0
     WHEN t.status IN ('created', 'assigned', 'failed', 'blocked', 'waiting_for_approval') THEN 0
     WHEN t.status = 'running' THEN 1
     ELSE 2 END"""

# `noqa: S608` -- the only interpolated value is `WAITING_ON_CASE`, a module constant
# defined thirty lines above. There is no request data anywhere in this string, which is
# the property the rule exists to enforce.
_WORK_COUNT_BY_BUCKET = f"""
SELECT {WAITING_ON_CASE} AS bucket, count(*) AS n
FROM tasks t
WHERE t.organization_id = CAST(:o AS varchar(40))
  AND (CAST(:state AS text) IS NULL OR t.status = :state)
GROUP BY 1
"""  # noqa: S608 -- the only interpolated value is `WAITING_ON_CASE`, a module constant

_WORK_QUEUE = _WORK_QUEUE.format(WAITING_ON_CASE=WAITING_ON_CASE)

_REPORT_TASK = """
SELECT t.id, t.title, t.goal, t.task_type, t.status, t.priority, t.requester_type,
       t.created_at, t.started_at, t.completed_at, t.deadline_at, t.attempt_count,
       t.failure_category, t.last_error, t.output, t.constraints,
       t.spent_tokens, t.budget_limit_tokens, t.root_task_id, t.parent_task_id,
       a.name AS owner_name
FROM tasks t
LEFT JOIN agents a ON a.id = t.owner_agent_id
WHERE t.organization_id = CAST(:o AS varchar(40)) AND t.id = :id
"""

_REPORT_DELEGATIONS = """
SELECT d.id, d.parent_task_id, d.child_task_id, d.status, d.objective, d.result,
       d.denial_reason, d.depth, d.delegation_path, d.created_at, d.accepted_at,
       d.completed_at, d.approval_id,
       src.name AS from_agent, tgt.name AS to_agent
FROM delegations d
LEFT JOIN agents src ON src.id = d.source_agent_id
LEFT JOIN agents tgt ON tgt.id = d.target_agent_id
WHERE d.organization_id = CAST(:o AS varchar(40))
  AND (d.parent_task_id = :id OR d.child_task_id = :id)
ORDER BY d.depth, d.created_at
"""

_REPORT_EXECUTIONS = """
SELECT x.id, x.task_id, x.status, x.model_used, x.model_profile, x.summary,
       x.error_kind, x.error_message, x.input_tokens, x.output_tokens, x.duration_ms,
       x.cost_usd, x.started_at, x.finished_at, x.artifacts,
       a.name AS agent_name
FROM executions x
LEFT JOIN agents a ON a.id = x.agent_id
WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.task_id = :id
ORDER BY x.created_at
"""

#: Every execution under the task, for the steps list. Separate from `_REPORT_EXECUTIONS`
#: on purpose: that one feeds the summary counts, which are defined over the head task,
#: and widening it would redefine what "3 ran" means on every report that ever rendered.
#: This one answers a different question — "what did each hand do, in order" — and so it
#: carries the task title with each row, so the page does not have to join them.
_REPORT_TREE_EXECUTIONS = """
SELECT x.id, x.task_id, t.title AS task_title, x.status, x.model_used, x.model_profile,
       x.summary, x.error_kind, x.error_message, x.input_tokens, x.output_tokens,
       x.duration_ms, x.cost_usd, x.started_at, x.finished_at, x.artifacts,
       a.name AS agent_name
FROM executions x
JOIN tasks t ON t.id = x.task_id AND t.organization_id = x.organization_id
LEFT JOIN agents a ON a.id = x.agent_id
WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.task_id = ANY(:ids)
ORDER BY x.created_at
"""

_REPORT_APPROVALS = """
SELECT ap.id, ap.task_id, ap.action_type, ap.status, ap.decision, ap.decision_note,
       ap.effect_class, ap.risk_level, ap.reason, ap.requested_by, ap.requested_by_type,
       ap.decided_by, ap.decided_at, ap.expires_at, ap.created_at
FROM approvals ap
WHERE ap.organization_id = CAST(:o AS varchar(40)) AND ap.task_id = :id
ORDER BY ap.created_at
"""

#: The ordered record. `events` is the platform's own log and it is the one thing here that
#: is written by everything rather than by any one part, which is why it is the last source
#: rather than a join onto the others.
_REPORT_EVENT_SCOPE = """
FROM events
WHERE organization_id = CAST(:o AS varchar(40))
  AND (split_part(subject, '.', 1) = ANY(:ids) OR data->>'task_id' = ANY(:ids))
"""
_REPORT_EVENTS = (
    "SELECT id, type, actor_id, source AS actor_type, occurred_at, subject, data "
    + _REPORT_EVENT_SCOPE
    + " ORDER BY occurred_at, id LIMIT :event_limit OFFSET :event_offset"
)
_REPORT_EVENT_COUNT = "SELECT count(*) " + _REPORT_EVENT_SCOPE


#: Everything underneath a task, by walking `root_task_id`. A report that only shows the
#: task you asked about and not the work it produced is a report about a shell.
_REPORT_TREE = """
WITH RECURSIVE scoped_ids(id) AS (
    SELECT id FROM tasks
    WHERE organization_id = CAST(:o AS varchar(40)) AND (id = :id OR root_task_id = :id)
    UNION
    SELECT child.id FROM tasks child JOIN scoped_ids parent ON child.parent_task_id = parent.id
    WHERE child.organization_id = CAST(:o AS varchar(40))
)
SELECT t.id, t.title, t.status, t.task_type, t.parent_task_id, t.created_at, t.completed_at,
       a.name AS owner_name
FROM tasks t JOIN scoped_ids scope ON scope.id = t.id
LEFT JOIN agents a ON a.id = t.owner_agent_id AND a.organization_id = t.organization_id
WHERE t.organization_id = CAST(:o AS varchar(40))
ORDER BY t.created_at, t.id
"""


#: `WAITING_ON_CASE` in SQL, as `(bucket, name)`. Adjacent to it on purpose: a change to
#: one is visibly a change to the other, which is the whole point of writing it once.
_BUCKETS: dict[int, str] = {0: "you", 1: "an_agent", 2: "nobody"}


async def ceo_work_queue(
    conn: ReadConnection,
    *,
    organization_id: str,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """The tasks a CEO can act on, with what is waiting on whom.

    The whole list, not a filtered subset. A CEO opening "what needs me" and finding a page
    that has already decided they need nothing is the failure this is built to avoid.
    """
    total = int(
        (await conn.execute(text(_WORK_COUNT), {"o": organization_id, "state": state})).scalar_one()
    )
    rows = (
        (
            await conn.execute(
                text(_WORK_QUEUE),
                {
                    "o": organization_id,
                    "state": state,
                    "limit": limit,
                    "offset": offset,
                },
            )
        )
        .mappings()
        .all()
    )

    # **Counted over the whole table, not over `rows`.**
    #
    # `rows` is the page. Counting it and labelling the result `needs_you` is how the
    # register came to show "Waiting on you 100" beside a list of 176 tasks and a
    # "With an agent: 0" tile while 67 tasks were assigned -- the three numbers described
    # different pages.
    counts: dict[str, int] = dict.fromkeys(_BUCKETS.values(), 0)
    for bucket, n in (
        await conn.execute(text(_WORK_COUNT_BY_BUCKET), {"o": organization_id, "state": state})
    ).all():
        counts[_BUCKETS[int(bucket)]] += int(n)

    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "goal": r["goal"],
                "parent_task_id": r["parent_task_id"],
                "root_task_id": r["root_task_id"],
                "task_type": r["task_type"],
                "status": r["status"],
                "priority": r["priority"],
                "requester_type": r["requester_type"],
                "owner_name": r["owner_name"],
                "created_at": r["created_at"],
                "deadline_at": r["deadline_at"],
                "delegated": int(r["delegated"] or 0),
                "children": int(r["children"] or 0),
                "executions": int(r["executions"] or 0),
                "approvals_waiting": int(r["approvals_waiting"] or 0),
                "failure_category": r["failure_category"],
                "last_error": r["last_error"],
                "waiting_on": _BUCKETS[int(r["waiting_bucket"])],
            }
            for r in rows
        ],
        "total": total,
        "needs_you": counts["you"],
        "in_flight": counts["an_agent"],
        "settled": counts["nobody"],
        "limit": limit,
        "offset": offset,
    }


async def task_report(
    conn: ReadConnection,
    *,
    organization_id: str,
    task_id: str,
    event_limit: int = 200,
    event_offset: int = 0,
) -> dict[str, Any]:
    """The account of one task: what was asked, and everything that happened to it.

    A 404 for another tenant's task, not a 403 — saying "forbidden" would confirm the row
    exists, and the composite keys exist so a reader cannot tell the difference.
    """
    head = (
        (await conn.execute(text(_REPORT_TASK), {"o": organization_id, "id": task_id}))
        .mappings()
        .one_or_none()
    )
    if head is None:
        raise NotFoundError(f"no task {task_id}")

    delegations = (
        (await conn.execute(text(_REPORT_DELEGATIONS), {"o": organization_id, "id": task_id}))
        .mappings()
        .all()
    )
    executions = (
        (await conn.execute(text(_REPORT_EXECUTIONS), {"o": organization_id, "id": task_id}))
        .mappings()
        .all()
    )
    approvals = (
        (await conn.execute(text(_REPORT_APPROVALS), {"o": organization_id, "id": task_id}))
        .mappings()
        .all()
    )
    tree = (
        (await conn.execute(text(_REPORT_TREE), {"o": organization_id, "id": task_id}))
        .mappings()
        .all()
    )
    tree_ids = {str(r["id"]) for r in tree}
    event_params = {
        "o": organization_id,
        "ids": sorted(tree_ids),
        "event_limit": event_limit,
        "event_offset": event_offset,
    }
    event_total = int((await conn.execute(text(_REPORT_EVENT_COUNT), event_params)).scalar_one())
    events = (
        (
            await conn.execute(
                text(_REPORT_EVENTS),
                event_params,
            )
        )
        .mappings()
        .all()
    )

    tree_executions = (
        (
            await conn.execute(
                text(_REPORT_TREE_EXECUTIONS),
                {"o": organization_id, "ids": sorted(tree_ids)},
            )
        )
        .mappings()
        .all()
    )

    # **Resolved names, not raw ids.** `enrich` mutates the frames in place, exactly as
    # it does for the stream, so the report's `view` and the stream's `view` are the
    # same projection of the same events.
    frames = [{"type": str(e["type"]), "data": dict(e["data"] or {})} for e in events]
    await enrich(frames, conn, organization_id)
    enriched = [dict(f.get("view") or {}) for f in frames]

    return {
        "available_actions": sorted(
            event.value for event in allowed_task_transitions(TaskStatus(head["status"]))
        ),
        "task": {
            "id": head["id"],
            "title": head["title"],
            "goal": head["goal"],
            "task_type": head["task_type"],
            "status": head["status"],
            "priority": head["priority"],
            "requester_type": head["requester_type"],
            "owner_name": head["owner_name"],
            "created_at": head["created_at"],
            "started_at": head["started_at"],
            "completed_at": head["completed_at"],
            "deadline_at": head["deadline_at"],
            "attempt_count": head["attempt_count"],
            "failure_category": head["failure_category"],
            "last_error": head["last_error"],
            "output": head["output"],
            "constraints": head["constraints"],
            "spent_tokens": head["spent_tokens"],
            "budget_limit_tokens": head["budget_limit_tokens"],
            "root_task_id": head["root_task_id"],
            "parent_task_id": head["parent_task_id"],
        },
        "summary": _summarise(head, delegations, executions, approvals),
        "delegations": [
            {
                "id": d["id"],
                "parent_task_id": d["parent_task_id"],
                "child_task_id": d["child_task_id"],
                "from_agent": d["from_agent"],
                "to_agent": d["to_agent"],
                "status": d["status"],
                "objective": d["objective"],
                "result": d["result"],
                "denial_reason": d["denial_reason"],
                "depth": d["depth"],
                "created_at": d["created_at"],
                "completed_at": d["completed_at"],
            }
            for d in delegations
        ],
        "tree": [
            {
                "id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "task_type": t["task_type"],
                "parent_task_id": t["parent_task_id"],
                "owner_name": t["owner_name"],
                "completed_at": t["completed_at"],
            }
            for t in tree
        ],
        "executions": [
            {
                "id": x["id"],
                "agent_name": x["agent_name"],
                "status": x["status"],
                "model_used": x["model_used"],
                "model_profile": x["model_profile"],
                "summary": x["summary"],
                "error_message": x["error_message"],
                "input_tokens": x["input_tokens"],
                "output_tokens": x["output_tokens"],
                "duration_ms": x["duration_ms"],
                "cost_usd": x["cost_usd"],
                "started_at": x["started_at"],
                "finished_at": x["finished_at"],
            }
            for x in executions
        ],
        "approvals": [
            {
                "id": a["id"],
                "action_type": a["action_type"],
                "status": a["status"],
                "decision": a["decision"],
                "decision_note": a["decision_note"],
                "effect_class": a["effect_class"],
                "risk_level": a["risk_level"],
                "reason": a["reason"],
                "requested_by": a["requested_by"],
                "requested_by_type": a["requested_by_type"],
                "decided_by": a["decided_by"],
                "decided_at": a["decided_at"],
                "expires_at": a["expires_at"],
            }
            for a in approvals
        ],
        # **The same projection the live stream uses**, so a refresh and the feed agree.
        #
        # The report returned raw `event_type` + `actor_id` while the stream returned
        # `view.from_agent` / `view.title`, so opening a task rendered
        # `task.delegation_accepted · agt_01m3…` — a type name and an opaque id — for the
        # same event the feed had already read as a sentence. `enrich` resolves the ids to
        # names here too, and everything display-only lives under `view`, exactly as in
        # the stream frames.
        "events": [
            {
                "id": e["id"],
                "event_type": e["type"],
                "actor_id": e["actor_id"],
                "actor_type": e["actor_type"],
                "subject": e["subject"],
                "occurred_at": e["occurred_at"],
                "data": e["data"] or {},
                "view": view,
            }
            # Same length by construction: one frame per row, in order.
            for e, view in zip(events, enriched, strict=True)
        ],
        "event_total": event_total,
        "event_limit": event_limit,
        "event_offset": event_offset,
        "tree_size": len(tree_ids),
        "tree_executions": [
            {
                "id": x["id"],
                "task_id": x["task_id"],
                "task_title": x["task_title"],
                "status": x["status"],
                "agent_name": x["agent_name"],
                "model_used": x["model_used"],
                "model_profile": x["model_profile"],
                "summary": x["summary"],
                "error_kind": x["error_kind"],
                "error_message": x["error_message"],
                "input_tokens": x["input_tokens"],
                "output_tokens": x["output_tokens"],
                "duration_ms": x["duration_ms"],
                "cost_usd": str(x["cost_usd"]) if x["cost_usd"] is not None else None,
                "started_at": x["started_at"],
                "finished_at": x["finished_at"],
                "artifacts": x["artifacts"] or [],
            }
            for x in tree_executions
        ],
    }


def _summarise(
    head: Any,
    delegations: list[Any],
    executions: list[Any],
    approvals: list[Any],
) -> dict[str, Any]:
    """The four numbers, and the one sentence a reader starts with.

    Written as a function rather than computed in the page so the dashboard and the report
    cannot disagree about the same task — which is the shape of every stale-tile defect
    this repository has recorded.
    """
    refused = [d for d in delegations if d["status"] in ("refused", "denied", "rejected")]
    decided = [a for a in approvals if a["decision"]]
    agents = {str(d["to_agent"]) for d in delegations if d["to_agent"]}

    # Counted **by status**, never as `total - failed`.
    #
    # `len(executions) - len(failed)` counts a `running` execution as a success, and that
    # is not a hypothetical: the seeded history carries four executions still marked
    # `running` on tasks that reached `failed` on 27 September. So the report said "1 ran,
    # 0 failed" for a task that failed -- the exact wrong-in-the-reassuring-direction
    # figure this repository keeps finding.
    #
    # A stuck execution is also *information*, not noise: it means a run was interrupted
    # and nothing closed it out. So it gets its own key rather than being folded into
    # either total.
    by_status: dict[str, int] = {}
    for x in executions:
        by_status[str(x["status"])] = by_status.get(str(x["status"]), 0) + 1
    return {
        "delegated_to": len(agents),
        "refused": len(refused),
        "executed": by_status.get("completed", 0),
        "failed": by_status.get("failed", 0),
        "in_flight": by_status.get("running", 0) + by_status.get("pending", 0),
        "started": by_status.get("started", 0),
        "attempts": len(executions),
        "approvals_total": len(approvals),
        "approvals_pending": len([a for a in approvals if a["status"] == "pending"]),
        "approvals_decided": len(decided),
        "models": sorted({str(x["model_used"]) for x in executions if x["model_used"]}),
        "outcome": str(head["status"]),
    }


__all__ = ["ceo_work_queue", "task_report"]
