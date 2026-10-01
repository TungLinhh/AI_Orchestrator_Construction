"""The six departments, and the tree that reports to the CEO.

## Why the shape changed

The product's navigation was built around **projects**: six construction projects, 240 WBS
nodes, 2778 progress readings. That is real and it is measured, and it is not what this
organisation is run on. What it is run on is **six departments with different scopes of
work** -- Procurement, HR, Sales, Finance, QA, Design -- and the question a manager opens the
page to answer is *what is each of those six doing right now*, not *how is that piling
going*.

So the hierarchy here is the organisation's, not the schedule's:

```
Executive Agent            the CEO
├── Procurement Agent      one of the six
├── HR Agent
├── Sales/BD Agent
├── Finance Agent
├── QA/QC-HSE Agent
├── Design/M&E Agent
└── the rest               Knowledge, Project Mgmt, and the block placeholders
```

The six are the departments the register names with a *business* scope. The rest are shown
as a second tier rather than hidden, because an agent that exists and cannot be seen is
worse than one that is not configured.

## What each box needs, and why these five

A person watching a fleet wants five things, and a box that shows fewer than five is a
decorative rectangle:

1. **Is it working right now** — the `running` light. Read from `executions`, not from a
   status column an operator has to remember to clear.
2. **What did it say** — the last run's `summary`, the same string a workflow would branch on.
3. **What it is doing** — the open tasks, **by title**, because "3 tasks" is not an answer
   and "checking the authority band" is.
4. **What it finished** — the same, with the outcome. A fleet with no memory looks broken.
5. **Whether it was told what to do and said no** — refusals are the most useful line in the
   whole picture and a box that hides them is a box that makes the platform look reliable.

`chain_of_thought` is deliberately **not** a field name. The model does not expose private
reasoning and this platform does not store it. What it stores is `decision_record`,
`artifacts`, the tool calls in the run, and the summary -- so the panel is called **"what it
did"** and shows those. Claiming to show reasoning the platform does not have would be the
one dishonest panel in the product.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text

from ai_orchestrator.application.ports import ReadConnection


def _now() -> float:
    """Wall clock, for the staleness comparison done in Python.

    The SQL already filters on the same threshold; this is for the per-run flag the page
    paints, and it is read once per response rather than once per row.
    """
    return time.time()


#: The department slugs, in the order a manager reads them.
#:
#: **The agent names and display labels that used to sit beside these slugs are
#: gone, and that is the fix.** This tuple was a third copy of the department list
#: -- `seed.DEPARTMENTS` builds the units, `application.scenarios` routes the work
#: -- and its two extra columns had drifted from both: it named `Sales/BD Agent`,
#: `QA/QC-HSE Agent` and `Design/M&E Agent` where the seed builds `Sales Agent`,
#: `Quality Agent` and `Design Agent`, and it labelled them `Mua sắm`, `Nhân sự`
#: and `Thiết kế & M&E` on an otherwise English page, `QA/QC-HSE` untranslated
#: even within the tuple.
#:
#: It decided membership. `second_tier` excluded the six *by name*, so the three
#: that had drifted failed their own exclusion and rendered as offices -- the
#: Offices band showed six boxes, three of them departments. And the department
#: band looked the same names up, so those three also rendered as "no agent named
#: X" placeholders. One department, two bands, one of them plainly wrong, and the
#: page is what told us.
#:
#: Both queries now ask `organizational_units.unit_type`, which the seed writes.
#: What remains here is the slug list, used only where a caller needs to name a
#: department, and it names nothing that can drift.
DEPARTMENT_SLUGS: tuple[str, ...] = (
    "procurement",
    "hr",
    "sales",
    "finance",
    "qa",
    "design",
)

#: The chief, by name. The one place a name is written down is where the
#: exclusion has to happen, so it is named once and used everywhere.
CHIEF_AGENT_NAME = "Executive Agent"

#: Every agent in a unit that has a parent unit -- the middle tier. Excludes the
#: chief and the six departments, which are rendered from their own lists.
_TREE_BY_TIER = """
SELECT a.id, a.name, a.lifecycle_status, a.runtime_status, a.health,
       a.autonomy_ceiling, a.granted_level, a.model_profile, a.kill_switch,
       u.name AS unit_name, u.id AS unit_id,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at > now() - make_interval(secs => :stuck)) AS running_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at <= now() - make_interval(secs => :stuck)) AS stuck_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id) AS runs
FROM agents a
JOIN organizational_units u ON u.id = a.org_unit_id AND u.organization_id = a.organization_id
WHERE a.organization_id = CAST(:o AS varchar(40))
  AND a.name <> CAST(:chief AS varchar(120))
  -- **Offices, and nothing else.** Asked of the unit's `unit_type` rather than by
  -- excluding a list of department agent names.
  --
  -- It used to exclude by `a.name = ANY(...)` over a six-name list, and that list
  -- was a hand-written roster whose agent names had drifted from the seed's -- it
  -- said `Sales/BD Agent`, `QA/QC-HSE Agent` and `Design/M&E Agent` where the
  -- seed builds `Sales Agent`, `Quality Agent` and `Design Agent`. Three
  -- departments therefore failed their own exclusion and rendered **as offices**:
  -- the Offices band showed six boxes, three of them departments.
  --
  -- A hand-written roster asserts a fact about the organisation that nothing
  -- checks, and the comment directly above this clause claimed the test *was*
  -- structural while the SQL was a list. `unit_type` is the fact, the seed writes
  -- it, and a department renamed or added cannot leak into the tier above it.
  --
  -- (No colon-prefixed identifiers in these comments: `text()` binds them whether
  -- or not SQL would see them, so a bind parameter can be conjured by prose.)
  AND u.unit_type = CAST(:office AS varchar(40))
"""

_TREE = """
SELECT a.id, a.name, a.lifecycle_status, a.runtime_status, a.health,
       a.autonomy_ceiling, a.granted_level, a.model_profile, a.kill_switch,
       u.name AS unit_name, u.id AS unit_id, u.slug AS unit_slug,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at > now() - make_interval(secs => :stuck)) AS running_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at <= now() - make_interval(secs => :stuck)) AS stuck_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id) AS runs
FROM agents a
JOIN organizational_units u ON u.id = a.org_unit_id AND u.organization_id = a.organization_id
WHERE a.organization_id = CAST(:o AS varchar(40))
  AND u.unit_type = CAST(:department AS varchar(40))
ORDER BY u.depth, u.name
"""

#: A run is **stuck** if it started longer ago than this and is still marked running.
#:
#: The measurement that forced the distinction: the development database holds executions
#: started on 27 September still marked `running` on tasks that reached `failed`. A box that
#: lit up for those would say "working" about work nobody is doing -- and a fleet where the
#: lights are always on is a fleet nobody watches.
#:
#: Twice the executor's lease, which is the honest threshold: a run is expected to finish
#: well inside one, so past two it is not working, it is stranded.
STUCK_AFTER_SECONDS = 3_600

#: The CEO is an agent like any other, so it is read the same way -- named rather than
#: special-cased, because a special case is a second code path for the same question.
_CHIEF = """
SELECT a.id, a.name, a.lifecycle_status, a.runtime_status, a.health,
       a.autonomy_ceiling, a.granted_level, a.model_profile, a.kill_switch,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at > now() - make_interval(secs => :stuck)) AS running_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id
           AND x.status = 'running'
           AND x.started_at <= now() - make_interval(secs => :stuck)) AS stuck_now,
       (SELECT count(*) FROM executions x
         WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = a.id) AS runs
FROM agents a
WHERE a.organization_id = CAST(:o AS varchar(40)) AND a.name = :name
LIMIT 1
"""

#: Open work, **by title**. The panel's third question is "what is it doing", and a count
#: does not answer it.
_TASKS = """
SELECT t.id, t.title, t.status, t.task_type, t.created_at, t.completed_at, t.last_error
FROM tasks t
WHERE t.organization_id = CAST(:o AS varchar(40)) AND t.owner_agent_id = :agent
ORDER BY
  CASE t.status WHEN 'running' THEN 0 WHEN 'assigned' THEN 1 WHEN 'created' THEN 2
                WHEN 'failed' THEN 3 ELSE 4 END,
  t.created_at DESC
LIMIT :limit
"""

#: What the agent did, from the run itself. `summary` is the string a workflow branches on,
#: so it is the honest answer to "what did it say"; `artifacts` and `decision_record` are the
#: structured residue.
_RUNS = """
SELECT x.id, x.task_id, x.status, x.summary, x.model_used, x.input_tokens,
       x.output_tokens, x.duration_ms, x.cost_usd, x.error_message, x.error_kind,
       x.decision_record, x.artifacts, x.started_at, x.finished_at
FROM executions x
WHERE x.organization_id = CAST(:o AS varchar(40)) AND x.agent_id = :agent
ORDER BY x.created_at DESC
LIMIT :limit
"""

#: The edges. `delegations.child_task_id` is NULL on every row the real executor writes
#: (F172), so the shape is read from `tasks.parent_task_id` -- the column that is true --
#: and the delegation supplies *who* and *whether it was accepted*.
_EDGES = """
SELECT t.id AS child_task_id, t.parent_task_id, t.title,
       d.to_agent_id, d.from_agent_id, d.status, d.objective, d.depth
FROM tasks t
LEFT JOIN LATERAL (
    SELECT dg.target_agent_id AS to_agent_id, dg.source_agent_id AS from_agent_id,
           dg.status, dg.objective, dg.depth
    FROM delegations dg
    WHERE dg.organization_id = CAST(:o AS varchar(40)) AND dg.parent_task_id = t.parent_task_id
    ORDER BY dg.created_at DESC LIMIT 1
) d ON true
WHERE t.organization_id = CAST(:o AS varchar(40)) AND t.parent_task_id IS NOT NULL
ORDER BY t.created_at DESC
LIMIT 200
"""


def _box(row: Any, *, running: bool) -> dict[str, Any]:
    """One agent, as the tree needs it. `running` drives the light on the edge."""
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "running": bool(running) or int(row["running_now"] or 0) > 0,
        # **Distinct from `running`**, and the reason is a measurement: four executions
        # started on 27 September are still marked running on tasks that reached `failed`.
        # Lighting a box for those says "working" about work nobody is doing.
        "stuck": int(row["stuck_now"] or 0),
        "killed": bool(row["kill_switch"]),
        "lifecycle_status": row["lifecycle_status"],
        "health": row["health"],
        "ceiling": row["autonomy_ceiling"],
        "granted": row["granted_level"],
        "model": row["model_profile"],
        "run_count": int(row["runs"] or 0),
    }


async def fleet_tree(
    conn: ReadConnection, *, organization_id: str, per_agent_limit: int = 8
) -> dict[str, Any]:
    """The whole fleet as a tree, with enough detail per box to answer the five questions.

    One round trip per concern rather than one enormous join: the counts are per agent, the
    open work is per agent, and a single query joining both would either multiply the rows
    or need a `json_agg` per agent. Three small reads beat one unreadable one, and the page
    needs all three.
    """
    chief_row = (
        (
            await conn.execute(
                text(_CHIEF),
                {
                    "o": organization_id,
                    "name": "Executive Agent",
                    "stuck": STUCK_AFTER_SECONDS,
                },
            )
        )
        .mappings()
        .one_or_none()
    )

    # The department tier, **derived from the tree**.
    #
    # It used to iterate a hand-written tuple of six `(key, agent_name, label)`
    # triples and look each name up. Three of those names had drifted from the
    # seed's, so three departments rendered as "no agent named Sales/BD Agent"
    # placeholders while the *same* three appeared in the offices band above --
    # one department, two bands, one of them plainly wrong. The tuple is also why
    # the boxes were labelled `Mua sắm`, `Nhân sự`, `Thiềt kế & M&E`: Vietnamese
    # display names next to an English page, with `QA/QC-HSE` never translated
    # even inside the tuple, so it was never a coherent localisation.
    #
    # Now: one query for the units of type `department`, each labelled with the
    # name the seed gave it. Adding, renaming or removing a department is a change
    # to the seed and needs no edit here, which is the only way a roster of six
    # stays true in a seventh department.
    rows = (
        (
            await conn.execute(
                text(_TREE),
                {
                    "o": organization_id,
                    "department": "department",
                    "stuck": STUCK_AFTER_SECONDS,
                },
            )
        )
        .mappings()
        .all()
    )

    offices: list[dict[str, Any]] = []
    for row in rows:
        box = _box(row, running=False)
        box.update(
            await _detail(
                conn,
                organization_id=organization_id,
                agent_id=box["id"],
                limit=per_agent_limit,
            )
        )
        box.update(
            {
                "key": str(row["unit_slug"]),
                "label": str(row["unit_name"]),
                "missing": False,
            }
        )
        offices.append(box)

    # The middle tier, found by the **shape of the tree** rather than by name.
    #
    # It used to ask for `Knowledge Agent` and `Project Mgmt Agent` -- two names
    # this organisation has never had -- so the query always returned nothing
    # and the office tier rendered as an empty band. The list looked right in the
    # source and was wrong in every deployment, which is the failure mode of a
    # hand-written roster: it asserts a fact about the org that nothing checks.
    #
    # The test is now structural: an agent whose unit has a parent is a tier-2
    # agent. That holds for this seed, for a re-seeded one, and for an operator
    # who adds a unit, because it reads the same tree the delegation graph does.
    tier_two = (
        (
            await conn.execute(
                text(_TREE_BY_TIER),
                {
                    "o": organization_id,
                    "chief": CHIEF_AGENT_NAME,
                    "office": "office",
                    "stuck": STUCK_AFTER_SECONDS,
                },
            )
        )
        .mappings()
        .all()
    )

    second_tier_boxes = []
    for row in tier_two:
        box = _box(row, running=False)
        box.update(
            await _detail(
                conn,
                organization_id=organization_id,
                agent_id=box["id"],
                limit=per_agent_limit,
            )
        )
        box["unit"] = str(row["unit_name"])
        second_tier_boxes.append(box)

    chief = None
    if chief_row is not None:
        chief = _box(chief_row, running=False)
        chief.update(
            await _detail(
                conn,
                organization_id=organization_id,
                agent_id=chief["id"],
                limit=per_agent_limit,
            )
        )

    edges = (await conn.execute(text(_EDGES), {"o": organization_id})).mappings().all()

    return {
        "chief": chief,
        "offices": offices,
        "second_tier": second_tier_boxes,
        "edges": [
            {
                "parent": e["parent_task_id"],
                "child": e["child_task_id"],
                "title": e["title"],
                "status": e["status"],
                "objective": e["objective"],
                "depth": e["depth"],
            }
            for e in edges
            if e["parent_task_id"]
        ],
    }


async def _detail(
    conn: ReadConnection, *, organization_id: str, agent_id: str, limit: int
) -> dict[str, Any]:
    """The five answers for one agent, from two reads.

    `open` and `done` are separated **by status, not by recency**: a task that failed is
    neither open work nor a finished thing, and filing it as done is how a fleet looks
    healthy while everything it touched is broken. It goes in `attention`.
    """
    task_rows = (
        (
            await conn.execute(
                text(_TASKS), {"o": organization_id, "agent": agent_id, "limit": limit * 3}
            )
        )
        .mappings()
        .all()
    )
    run_rows = (
        (await conn.execute(text(_RUNS), {"o": organization_id, "agent": agent_id, "limit": limit}))
        .mappings()
        .all()
    )

    open_tasks, done_tasks, attention = [], [], []
    for t in task_rows:
        item = {
            "id": str(t["id"]),
            "title": str(t["title"]),
            "status": str(t["status"]),
            "task_type": t["task_type"],
            "error": t["last_error"],
        }
        if t["status"] in ("created", "assigned", "running"):
            open_tasks.append(item)
        elif t["status"] == "completed":
            done_tasks.append(item)
        else:
            attention.append(item)

    runs = [
        {
            "id": str(r["id"]),
            "task_id": r["task_id"],
            "status": str(r["status"]),
            # The same string a workflow branches on, so the panel cannot say one thing and
            # the engine another.
            "summary": r["summary"] or "",
            "model_used": r["model_used"],
            "tokens": int((r["input_tokens"] or 0) + (r["output_tokens"] or 0)),
            "duration_ms": r["duration_ms"],
            "error_message": r["error_message"],
            "error_kind": r["error_kind"],
            "artifacts": r["artifacts"],
            "decision_record": r["decision_record"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "running": str(r["status"]) == "running",
            "stuck": bool(
                r["status"] == "running"
                and r["started_at"] is not None
                and (r["started_at"].timestamp() < _now() - STUCK_AFTER_SECONDS)
            ),
        }
        for r in run_rows
    ]

    return {
        # `run_count` is the number; `runs` is the list. They were the same key in the first
        # draft and `box.update(detail)` replaced the count with the list, so a box showed
        # no number at all.
        "open_tasks": open_tasks[:limit],
        "done_tasks": done_tasks[:limit],
        "attention": attention[:limit],
        "counts": {
            "open": len(open_tasks),
            "done": len(done_tasks),
            "attention": len(attention),
        },
        "runs": runs,
        "last_run": runs[0] if runs else None,
    }


async def agent_detail(
    conn: ReadConnection, *, organization_id: str, agent_id: str, limit: int = 20
) -> dict[str, Any]:
    """One agent in full, for the panel a click opens."""
    detail = await _detail(conn, organization_id=organization_id, agent_id=agent_id, limit=limit)
    return {"id": agent_id, **detail}


__all__ = ["DEPARTMENT_SLUGS", "agent_detail", "fleet_tree"]
