"""Construction endpoints — the product, as opposed to the substrate.

Tập 1, §Tầng 1 asks for *"giao diện hội thoại & dashboard cho từng vai trò (CEO
cockpit, PM workspace, phê duyệt HITL trên mobile)"*. The three views it names are
`application/role_views.py`, and this module is the only thing that makes them
reachable by anything other than a Python process.

Until this file existed the API had **72 operations and not one of them touched a
project, a WBS, a progress reading, a contract, a supplier, a purchase order, an RFQ,
a tender, a zone or a gate.** Six projects, 240 WBS nodes and 2778 progress readings
were in the database, and the only way to see any of it was to open a Python shell.
That is the shape of a library with no consumer, and it is the reason this module
carries so much of its own argument in comments: a reader needs to know that these
endpoints exist, because otherwise the next tranche re-derives them.

## What each operation is, and what it is not

These are **transports**. Every judgement — what counts as late, what counts as
unmeasured, which projects belong in a portfolio — belongs to `role_views.py` and its
18 tests. This file adds HTTP, and it is held to one rule: **it must not lose a
distinction the read makes.**

Concretely, that means:

* `unmeasured` is its own list in the workspace response, and it is not merged into
  `late` or into a `zones` array the client is expected to filter. A client that
  received only `late` would report an unmeasured project as on schedule, which is
  the single most damaging thing this domain could do. The corpus makes it a live
  hazard: **100% of the real file's actual columns are byte-identical to its planned
  ones**, so "has a date" is not a measurement.
* `actual_updated` travels with every reading. Without it a consumer reads the
  corpus's copied planned date as an observation.
* An empty result is a 200 with an empty list, never a 404. A tenant with no projects
  has no projects, and that is not a missing resource.
* A project id from another tenant is a **404, not a 403**. Saying "forbidden" would
  confirm the row exists.

## Read-only, deliberately

Every operation here is a `GET`. Nothing in this module writes. The construction
writers — `project_operations`, `wbs_operations`, `progress_operations` — are reached
through the ingest pipeline and through agents, and an HTTP write path for them would
need the same refusal vocabulary, the same provenance columns and the same
review-as-an-agent-act questions that the writers were built with. That is Phase 6
work, and until it is done the honest answer is that this module cannot corrupt
anything.

## Tenancy

The organization comes from `ApiContext`, which comes from the authenticated
principal — never from a query parameter. Every statement also carries
`organization_id = CAST(:o AS varchar(40))`, and RLS is `FORCE`d on the tables read
here, so the explicit predicate is not belt and braces: it is what makes a
cross-tenant read *testable*, because the test role is `BYPASSRLS` and without the
predicate the test would be asserting nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query

from ai_orchestrator.api.deps import MAX_PAGE_SIZE, ApiContext, get_context, paginate
from ai_orchestrator.application.progress_operations import readings_for_node
from ai_orchestrator.application.project_operations import list_projects
from ai_orchestrator.application.role_views import ceo_cockpit, hitl_inbox, pm_workspace
from ai_orchestrator.application.wbs_operations import node_by_id, wbs_tree
from ai_orchestrator.domain.errors import NotFoundError

router = APIRouter(tags=["construction"])


def _iso(value: object) -> str | None:
    """A date or datetime as an ISO string, or `None`.

    `list_projects` returns `list[dict[str, object]]` because a `RowMapping` has no
    single useful type. Narrowing happens **here**, at the transport boundary, rather
    than with a `cast` at four call sites: a cast would silence the checker without
    checking anything, and these two helpers are the whole of the conversion a client
    actually needs.
    """
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return None


def _num(value: object) -> float | None:
    """A `numeric` column as a float, or `None`.

    `None` and not `0.0`: a project whose contract value was never recorded is a
    different fact from one worth nothing, and collapsing them is how a portfolio
    total ends up quietly wrong.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        # `Decimal` arrives as a string through some drivers; a numeric that is not a
        # number is a data problem, and returning `None` says "not recorded" rather
        # than inventing a value.
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _require_project(projects: list[dict[str, object]], project_id: str) -> dict[str, object]:
    """Find a project, or say plainly that there is not one.

    Deliberately a scan of the already-fetched list rather than a second query: the
    list is tenant-scoped, so "not in it" already means "not in this tenant", and a
    separate `SELECT ... WHERE id = :id` would be a second place for the tenant
    predicate to be wrong.
    """
    for project in projects:
        if project.get("id") == project_id:
            return project
    msg = f"no project {project_id!r} in this organization"
    raise NotFoundError(msg, resource_type="project", resource_id=project_id)


@router.get("/portfolio")
async def portfolio(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The CEO's cockpit: every project, plus how much has been delegated.

    Carries the agent posture, and the number to watch is `above_l1` — the count of
    agents granted more than their ceiling. It is **0** today and is meant to stay
    near 0 for a while: migration `0016` defaults every agent to L1, and only a
    measured shadow agreement rate should raise that. A dashboard that hid it would
    hide the one control that says the autonomy ladder is not being climbed for
    convenience.
    """
    return (await ceo_cockpit(ctx.session, organization_id=ctx.organization_id)).as_dict()


@router.get("/hiring")
async def hiring(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The recruitment process, as a person watching it happen.

    Sits beside `/departments` rather than under `/tasks` because the unit is a **process**, not
    a task: an operator's question is "how far along is the hire", and answering that from the
    task list means counting eight rows by hand and knowing which eight.

    Every stage carries the dossier's own vocabulary -- the SOP code, the office and department
    from the org chart, the gate, and the question the person is being asked. **No verdict**: no
    percentage, no "on track". The stages are not equally weighted and the process's shape is
    the thing an operator is meant to be able to change, so a number implying otherwise would be
    the one dishonest figure on the page.
    """
    from ai_orchestrator.application.hiring_view import hiring_process

    return await hiring_process(ctx.session, organization_id=ctx.organization_id)


@router.get("/departments")
async def departments(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The six departments, and the tree that reports to the CEO.

    Sits beside `/ceo/work` rather than under it because the question is different: that one
    is *what needs me*, and this is *what is each department doing*. The answer is per
    department, not per task -- an organisation with six departments and different scopes
    wants to watch the six, and a per-task list makes them reconstruct that by hand.

    Every box carries five things a person watching a fleet actually asks: whether it is
    working right now, what it last said, what it has open, what it finished, and what it
    refused. A box with fewer than five is a decorative rectangle.

    `stuck` is deliberately distinct from `running`. Four executions in this tenant started
    on 27 September are still marked running on tasks that reached `failed`, and a light that
    comes on for those reports work nobody is doing.
    """
    from ai_orchestrator.application.fleet_view import fleet_tree

    return await fleet_tree(ctx.session, organization_id=ctx.organization_id)


@router.get("/ceo/work")
async def ceo_work(
    state: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The tasks a CEO can act on, and what is waiting on whom.

    Sits beside `/portfolio` rather than under `/tasks` because the question is the CEO's
    and not a task's: *what needs me*. Every row carries `waiting_on` -- `you`,
    `an_agent`, or `nobody` -- and `needs_you` is the count of the first. That count is the
    reason to open the page, so it is computed here and in `ceo_work_queue`, and never in
    the browser.

    The **whole** list, not a filtered subset. A page that has already decided nobody needs
    anything is the failure this exists to prevent.
    """
    from ai_orchestrator.application.coordination import ceo_work_queue

    return await ceo_work_queue(
        ctx.session,
        organization_id=ctx.organization_id,
        state=state,
        limit=limit,
        offset=offset,
    )


@router.get("/projects")
async def projects(
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The project's projects, by name.

    An empty tenant gets `items: []` and `total: 0`, not a 404.
    """
    rows = await list_projects(ctx.session, organization_id=ctx.organization_id)
    return paginate(
        [
            {
                "id": r["id"],
                "code": r["code"],
                "name": r["name"],
                "address": r["address"],
                "status": r["status"],
                "contract_value": _num(r["contract_value"]),
                "currency_code": r["currency_code"],
                "planned_start": _iso(r["planned_start"]),
                "planned_completion": _iso(r["planned_completion"]),
            }
            for r in rows
        ],
        limit,
        offset,
    )


@router.get("/projects/{project_id}")
async def project(project_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """One project, with its node count.

    The count is here rather than only in the cockpit because a PM opening one project
    should not have to fetch the whole portfolio to find out whether the breakdown has
    been loaded yet.
    """
    rows = await list_projects(ctx.session, organization_id=ctx.organization_id)
    found = _require_project(rows, project_id)
    nodes = await wbs_tree(ctx.session, organization_id=ctx.organization_id, project_id=project_id)
    return {
        "id": found["id"],
        "code": found["code"],
        "name": found["name"],
        "address": found["address"],
        "status": found["status"],
        "contract_value": _num(found["contract_value"]),
        "currency_code": found["currency_code"],
        "wbs_nodes": len(nodes),
    }


@router.get("/projects/{project_id}/workspace")
async def workspace(project_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The PM's view of one project: every zone, which are late, which are unmeasured.

    **The three lists are the response.** `zones` is everything in sequence order,
    `late` is measured-and-behind worst first, and `unmeasured` is has-a-plan-and-no-
    actual. A client is expected to render all three, and to render `unmeasured` as a
    state of its own rather than as "on time" — which is not a styling preference, it
    is the difference between a project that was reported on and one that was not.

    A project with four late zones and two unmeasured ones is a different situation
    from one with six late, and a response that carried only `late` would let somebody
    conclude the second.
    """
    rows = await list_projects(ctx.session, organization_id=ctx.organization_id)
    _require_project(rows, project_id)
    view = await pm_workspace(
        ctx.session, organization_id=ctx.organization_id, project_id=project_id
    )
    return view.as_dict()


@router.get("/projects/{project_id}/wbs")
async def breakdown(project_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The work breakdown: a **flat** list with `parent_id`, in sequence order.

    Flat because a three-level tree has no natural first page, and a transport that
    cannot paginate forces every client to flatten it differently. Assembling the
    hierarchy is the client's business.
    """
    rows = await list_projects(ctx.session, organization_id=ctx.organization_id)
    _require_project(rows, project_id)
    nodes = await wbs_tree(ctx.session, organization_id=ctx.organization_id, project_id=project_id)
    return {
        "items": nodes,
        "count": len(nodes),
        "roots": sum(1 for n in nodes if n.get("parent_id") is None),
    }


@router.get("/projects/{project_id}/activity")
async def activity(
    project_id: str,
    wbs_id: str | None = Query(default=None, description="One node. Omit for the whole project."),
    report_ref: str | None = Query(default=None, description="One progress report."),
    limit: int = Query(200, ge=1, le=1000),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Progress readings, in the order the sheet had them.

    Ordered by `source_row` — the identity migration `0018` introduced. `line_label`
    repeats under every system heading, and `TĐ Hạ Tầng.xlsx` lists the same activity
    twice at different rows, so any other order either reorders or drops rows the file
    genuinely contains.

    **`actual_updated` is in every row and must be read.** It is the difference between
    "the sheet recorded this finished on the 14th" and "the sheet copied the planned
    date into the actual column", and for the whole real corpus those are the same
    date. A client that ignores the flag renders 2778 fabricated measurements.
    """
    rows = await list_projects(ctx.session, organization_id=ctx.organization_id)
    _require_project(rows, project_id)
    if wbs_id is not None:
        node = await node_by_id(ctx.session, organization_id=ctx.organization_id, node_id=wbs_id)
        # Two refusals, and the second is the one that was missing. A node that exists
        # in *another project of the same tenant* is a 404 here, not a 200 full of
        # somebody else's schedule: the path says which project is being asked about,
        # and a node that does not belong to it does not answer that question.
        #
        # Within a tenant this is not a security boundary -- the caller can reach the
        # other project through its own URL. It is a correctness one, and it is the
        # kind that makes `GET /projects/A/activity?wbs_id=<B's node>` return a
        # plausible-looking answer to a question nobody asked.
        if node is None or node.get("project_id") != project_id:
            msg = f"no work package {wbs_id!r} in project {project_id!r}"
            raise NotFoundError(msg, resource_type="wbs", resource_id=wbs_id)
    readings = await readings_for_node(
        ctx.session,
        organization_id=ctx.organization_id,
        node_id=wbs_id,
        report_ref=report_ref,
        limit=limit,
    )
    measured = sum(1 for r in readings if r.get("actual_updated"))
    return {
        "items": [_reading_json(r) for r in readings],
        "count": len(readings),
        "measured": measured,
        # Reported rather than computed by the client, because "how much of this was
        # actually measured" is the question the corpus makes urgent and the answer is
        # a count over rows, not something to re-derive per render.
        "unmeasured": len(readings) - measured,
        "limit": limit,
    }


def _reading_json(row: dict[str, object]) -> dict[str, Any]:
    """Dates as ISO strings; `numeric` as float; everything else unchanged.

    `actual_updated` is passed through explicitly rather than by `**row` so that its
    presence is a visible decision in the code and not an accident of the SELECT list.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dt.datetime | dt.date):
            out[key] = value.isoformat()
        elif isinstance(value, bool) or value is None:
            # `bool` before the numbers: `isinstance(True, int)` is true in Python, and
            # `is_adequate` / `actual_updated` are booleans that would silently become
            # 1 and 0.
            out[key] = value
        elif key.endswith(("_ratio", "_days")) and isinstance(value, int | float):
            out[key] = value
        else:
            out[key] = value
    return out


@router.get("/approvals/inbox")
async def inbox(
    now: dt.datetime | None = Query(
        default=None,
        description="Clock for wait times. Defaults to now; pass a value to make a "
        "screenshot reproducible.",
    ),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The mobile HITL queue: everything pending, oldest first.

    Oldest first because an approval queue has no urgency field and a person working
    it by insertion order is working it by accident. The dossier's autonomy ladder
    governs *who* may approve; this list is what is waiting, not a recommendation about
    what to do next, and it does not pretend otherwise.

    Nothing is filtered out. An approval naming a requester who no longer exists is
    listed with that requester, because a queue that silently omits rows cannot be
    reconciled against `approvals` — and a person deciding on a queue they cannot count
    is deciding on a queue they do not trust.

    `now` is a parameter so a report of this queue is reproducible; the same reason
    `hitl_inbox` takes one.
    """
    at = now or dt.datetime.now(tz=dt.UTC)
    if at.tzinfo is None:
        msg = "now must carry a timezone offset; approvals.created_at is timestamptz"
        raise ValueError(msg)
    pending = await hitl_inbox(ctx.session, organization_id=ctx.organization_id, now=at)
    return {
        "items": [a.as_dict(now=at) for a in pending],
        "count": len(pending),
        "as_of": at.isoformat(),
    }


__all__ = ["router"]
