"""The seven departments, and the tree that reports to the CEO.

## Why the shape changed

The product's navigation was built around **projects**: six construction projects, 240 WBS
nodes, 2778 progress readings. That is real and it is measured, and it is not what this
organisation is run on. What it is run on is **seven departments with different scopes of
work** -- Procurement, HR, Sales, Finance, QA, Design -- and the question a manager opens the
page to answer is *what is each of those departments doing right now*, not *how is that piling
going*.

So the hierarchy here is the organisation's, not the schedule's:

```
Executive Agent            the CEO
├── Procurement Agent      one of the departments
├── HR Agent
├── Sales/BD Agent
├── Finance Agent
├── QA/QC-HSE Agent
├── Design/M&E Agent
├── IT Agent               the seventh; added when BO-IT-SOP-007 had no home
└── the rest               Knowledge, Project Mgmt, and the block placeholders
```

Those six are the departments the register names with a *business* scope. The rest are shown
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
from collections.abc import Sequence
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
#: It decided membership. `second_tier` excluded them *by name*, so the three
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
#: chief and the seven departments, which are rendered from their own lists.
_TREE_BY_TIER = """
SELECT a.id, a.name, a.lifecycle_status, a.runtime_status, a.health,
       a.autonomy_ceiling, a.granted_level, a.model_profile, a.kill_switch,
       u.name AS unit_name, u.id AS unit_id, u.slug AS unit_slug,
       p.name AS parent_unit_name, p.slug AS parent_unit_slug,
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
LEFT JOIN organizational_units p
       ON p.id = u.parent_id AND p.organization_id = u.organization_id
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
       -- **The parent link the whole console was missing.**
       --
       -- `organizational_units.parent_id` was correct the whole time -- company at depth
       -- 0, three offices at depth 1, seven departments at depth 2 -- and this query
       -- simply never joined it, so `parent_unit_slug` was `None` for all seven
       -- departments. Measured before the fix: 7 of 7 `None`.
       --
       -- The consequence was not cosmetic. `renderTree` draws three bands from three
       -- flat lists because it has nothing to nest, and no test asserted that a
       -- department sits under its office, so a hierarchy that was flat in the payload
       -- was flat on the screen while 100 console checks passed.
       --
       -- `LEFT JOIN` rather than `JOIN`: a department with no parent would then vanish
       -- from the console entirely, which is worse than being shown at the top level
       -- where its brokenness is visible.
       p.name AS parent_unit_name, p.slug AS parent_unit_slug,
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
LEFT JOIN organizational_units p
       ON p.id = u.parent_id AND p.organization_id = u.organization_id
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
#:
#: The unit columns are read here for the same reason they are read for every other
#: box: the chief is a unit too, and a payload that names the chief's office but not
#: the chief's own unit is the same omission one tier down.
_CHIEF = """
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
LEFT JOIN organizational_units u
       ON u.id = a.org_unit_id AND u.organization_id = a.organization_id
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


def _identity(row: Any, *, fallback_key: str) -> dict[str, Any]:
    """The three fields every box needs to be a *unit a person can open*.

    **Written once because the omission was tier-specific.** Departments arrived with
    `key` and `label` because a hand-written tuple supplied them; offices got only
    `unit`; the chief got nothing. The consequence was that an office could not be
    routed to, because the detail panel is keyed by `key`, so the office tier existed
    as a band of boxes that could only be watched, never opened. That is a tier that is
    *drawn* rather than *tracked*, and the request was for units that are tracked as
    their own units.

    Measured before this function existed: office missing `key` and `label`, chief
    missing `key`, `label` and `unit`. Now all eleven boxes carry all three, and
    `test_the_keys_are_unique_across_the_whole_organisation` holds them to it --
    because `key` is a route segment, and a collision means one box opens the other.
    """
    # The chief's query and the office's query both read `unit_slug`; a defensive
    # `.get` costs nothing and keeps this usable from either shape.
    slug = row.get("unit_slug") or None
    name = str(row["unit_name"]) if row.get("unit_name") else None
    return {
        "key": str(slug or fallback_key),
        "label": str(name or row["name"]),
        "unit": str(name or row["name"]),
    }


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


def _count_of(box: dict[str, Any], field: str) -> int:
    """One field of a box's `counts`, defaulting to zero rather than raising."""
    return int((box.get("counts") or {}).get(field, 0) or 0)


def _roll_up(owner: dict[str, Any], children: Sequence[dict[str, Any]]) -> dict[str, int]:
    """What a unit is responsible for: its own, plus everything under it.

    `running` is taken as "lit or stale", because that is what a person watching the
    panel is asking -- is anything here moving -- and `runs` is the sum of both since
    a parent that has never run itself is still responsible for the runs beneath it.
    """
    return {
        "agents": 1 + len(children),
        "own": 1,
        "open": _count_of(owner, "open") + sum(_count_of(c, "open") for c in children),
        "done": _count_of(owner, "done") + sum(_count_of(c, "done") for c in children),
        "attention": _count_of(owner, "attention")
        + sum(_count_of(c, "attention") for c in children),
        "running": int(bool(owner.get("running")) or bool(owner.get("stuck")))
        + sum(1 for c in children if c.get("running") or c.get("stuck")),
        "runs": int(owner.get("run_count") or 0)
        + sum(int(c.get("run_count") or 0) for c in children),
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
        box.update(_identity(row, fallback_key=f"dept-{len(offices) + 1}"))
        box.update(
            {
                "parent_unit_slug": (
                    str(row["parent_unit_slug"]) if row["parent_unit_slug"] else None
                ),
                "parent_unit_name": (
                    str(row["parent_unit_name"]) if row["parent_unit_name"] else None
                ),
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

    second_tier_boxes: list[dict[str, Any]] = []
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
        box.update(_identity(row, fallback_key=f"office-{len(second_tier_boxes) + 1}"))
        box["parent_unit_slug"] = str(row["parent_unit_slug"]) if row["parent_unit_slug"] else None
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
        # `chief` is the literal the page routes on, and it is fixed on both sides, so
        # it is passed rather than derived: the chief's unit slug is the company root,
        # and routing the detail panel to `/agent/company` would be a second name for
        # the same agent and a route nobody else uses.
        chief.update(_identity(chief_row, fallback_key="chief"))
        chief["key"] = "chief"

    # **Roll-up.** An office's own numbers plus its departments', and the chief's own
    # plus every office's.
    #
    # Without this an office reports only its own activity -- usually zero, because an
    # office delegates rather than executes -- so the tier reads as idle while its
    # departments are busy. Measured: the three offices together own none of the open
    # tasks; all of it sits one tier down. The office panel is therefore built from
    # both, and `test_an_office_rolls_up_exactly_its_own_departments` holds the sum to
    # the departments rather than trusting the arithmetic.
    for office in second_tier_boxes:
        children = [b for b in offices if b.get("parent_unit_slug") == office["key"]]
        office["rollup"] = _roll_up(office, children)

    if chief is not None:
        chief["rollup"] = _roll_up(chief, [*second_tier_boxes, *offices])

    # **The nested tree, built server-side.**
    #
    # The page used to reassemble the hierarchy from three flat lists, which is how a
    # payload without parent links became a picture without grouping. Building it here
    # means the shape is asserted by
    # `tests/integration/test_departments_reports_the_tree.py` rather than inferred by
    # a page check that could only string-match.
    #
    # A department whose `parent_unit_slug` names no office in the payload is placed in
    # `unassigned` instead of being dropped. Silently discarding it would make the
    # console look complete while hiding an agent that exists -- the "confident answer
    # with a wrong number" shape this project keeps meeting.
    grouped: dict[str, list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    office_keys = {b["key"] for b in second_tier_boxes}
    for dept in offices:
        parent = dept.get("parent_unit_slug")
        if parent in office_keys:
            grouped.setdefault(str(parent), []).append(dept)
        else:
            unassigned.append(dept)
    tree_offices = [
        {**office, "departments": grouped.get(office["key"], [])} for office in second_tier_boxes
    ]

    edges = (await conn.execute(text(_EDGES), {"o": organization_id})).mappings().all()

    return {
        "chief": chief,
        "offices": offices,
        "second_tier": second_tier_boxes,
        # Flat lists stay: two surfaces still read them and replacing them here would
        # break a working view to serve a new one.
        "tree": {
            "chief": chief,
            "offices": tree_offices,
            "unassigned": unassigned,
            "total_agents": (1 if chief else 0) + len(second_tier_boxes) + len(offices),
        },
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
