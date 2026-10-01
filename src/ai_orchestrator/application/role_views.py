"""The three role-shaped reads Tầng 1 asks for.

Tập 1, §Tầng 1: *"giao diện hội thoại & dashboard cho từng vai trò (CEO cockpit, PM
workspace, phê duyệt HITL trên mobile)"* — a conversational interface and dashboard per
role: CEO cockpit, PM workspace, HITL approval on mobile.

So the reads are **role-shaped questions**, not per-table CRUD. `GET /projects/{id}`
is not a dashboard; "which of my four zones is late, and by how much" is. Every function
here is a question somebody in that role actually opens the product to ask, and each
one names what it refuses to count as an answer — because the alternative is a number
that looks like a finding and is not.

## The three questions, and what each of them refuses

**CEO cockpit** — "where does the portfolio stand?" It answers with gates, schedule
slip and autonomy in one place, because a CEO's question is never about one of those.
It **excludes projects with no schedule readings** from the slip figure rather than
counting them as on time: `progress_snapshots` distinguishes "no actual was recorded"
from "finished on the planned day", and an endpoint that cannot tell those apart
reports an unmeasured project as entirely on schedule. That is the F99 shape and it is
the single most damaging thing this module could do.

**PM workspace** — "which zone is late?" One query per zone, with the lateness in days,
and zones that have never been measured listed separately from zones that are on time.
Both lists are returned, because "on time" and "not measured" are different facts and a
list of thirty zeros is not a lateness report.

**HITL inbox** — "what am I being asked to approve?" Pending approvals, oldest first,
with how long each has been waiting. It **excludes nothing** and **invented nothing**:
an approval with no requester is listed with a null requester rather than dropped,
because a queue that silently omits rows cannot be reconciled against `approvals`.

## Where the numbers come from

All three are tenant-scoped by RLS, and every statement also carries
`organization_id = :o` — not belt and braces, because the application role is not
`BYPASSRLS` and the test role is, so the explicit predicate is the only thing that
makes a cross-tenant read impossible in a test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text

from ai_orchestrator.application.ports import ReadConnection

# --- CEO cockpit ---------------------------------------------------------------

_PORTFOLIO = """
SELECT p.id, p.code, p.name, p.status, p.contract_value, p.currency_code,
       p.planned_start, p.planned_completion,
       (SELECT count(*) FROM wbs w WHERE w.organization_id = p.organization_id
          AND w.project_id = p.id) AS wbs_nodes,
       (SELECT count(*) FROM gate_instances g WHERE g.organization_id = p.organization_id
          AND g.subject_id = p.id) AS gates_open,
       (SELECT count(*) FROM gate_decisions d JOIN gate_instances g
          ON g.id = d.gate_instance_id AND g.organization_id = d.organization_id
          WHERE d.organization_id = p.organization_id AND g.subject_id = p.id) AS gates_decided
FROM projects p
WHERE p.organization_id = CAST(:o AS varchar(40))
ORDER BY p.name
"""

#: The one that matters. `actual_updated IS TRUE` is the filter that stops an
#: unmeasured project reading as an on-schedule one -- the corpus's progress sheets
#: record planned dates and leave the actual columns empty, so 100% of the real file is
#: unmeasured, and every one of those activities has a variance of zero.
#: One position per work package, taken from its **newest measured reading**.
#:
#: F125, and it took two attempts. The first version filtered the aggregate on
#: `actual_updated IS TRUE`; that was necessary and not sufficient. The second version
#: still computed `max(actual_finish_on) - max(planned_finish_on)`, and those are two
#: **independent** aggregates: nothing makes them come from the same row. On the real
#: corpus node `S028` has a measured reading from March and another from May, so
#: `max(actual)` was March and `max(planned)` was May, and the difference came out as
#: **-65 days** -- a zone that is 2 days late reported as 65 days early, while the
#: `late` list, built on a different basis, called the same node 2 days late.
#:
#: So the basis is fixed: a `LATERAL` picks the newest measured reading per node, and
#: the slip is *that row's* actual against *that row's* planned. Two numbers from one
#: row, which is the only way a difference between them means anything.
#:
#: `readings` and `measured` stay as counts over all of a node's readings -- they answer
#: "how much is there", where the lateral answers "where does it stand".
_ZONE_POSITION = """
SELECT w.id, w.code, w.name, w.parent_id,
       (SELECT count(*) FROM progress_snapshots s
         WHERE s.wbs_id = w.id AND s.organization_id = w.organization_id) AS readings,
       (SELECT count(*) FROM progress_snapshots s
         WHERE s.wbs_id = w.id AND s.organization_id = w.organization_id
           AND s.actual_updated IS TRUE) AS measured,
       m.source_row          AS position_row,
       m.actual_finish_on    AS last_actual_finish,
       m.planned_finish_on   AS last_planned_finish,
       (m.actual_finish_on - m.planned_finish_on) AS slip_days
FROM wbs w
LEFT JOIN LATERAL (
    SELECT p.source_row, p.actual_finish_on, p.planned_finish_on
    FROM progress_snapshots p
    WHERE p.wbs_id = w.id AND p.organization_id = w.organization_id
      AND p.actual_updated IS TRUE
    ORDER BY p.source_row DESC
    LIMIT 1
) m ON true
WHERE w.organization_id = CAST(:o AS varchar(40)) AND w.project_id = :project
ORDER BY w.sequence
"""

#: Zones whose newest measured reading finished after its own planned day.
#:
#: **Same basis as `_ZONE_POSITION`**, deliberately. It was a separate `GROUP BY` with
#: its own `max()`s, which is how two queries in one module came to disagree about one
#: node (F125). This one is a filter over the same lateral, so `late` is by
#: construction a subset of `zones` and the two cannot drift.
_LATE_ZONES = """
SELECT w.code, w.name, (m.actual_finish_on - m.planned_finish_on) AS slip_days
FROM wbs w
JOIN LATERAL (
    SELECT p.actual_finish_on, p.planned_finish_on
    FROM progress_snapshots p
    WHERE p.wbs_id = w.id AND p.organization_id = w.organization_id
      AND p.actual_updated IS TRUE
    ORDER BY p.source_row DESC
    LIMIT 1
) m ON true
WHERE w.organization_id = CAST(:o AS varchar(40)) AND w.project_id = :project
  AND m.actual_finish_on > m.planned_finish_on
ORDER BY slip_days DESC
"""

#: The dashboard's delegation control.
#:
#: `above_l1` counts agents granted **more** than their own ceiling. That is a violation and
#: nothing else, and it is the number the tile is built to alarm on.
#:
#: The first version said `granted_level <> autonomy_ceiling`, which is *any* difference, and
#: the page reported **8** on a database where every agent sits at `L1` and every ceiling is
#: `L1` or higher — eight is exactly the number of register agents whose derived ceiling had
#: been corrected away from `L1`. So the control alarmed on the correct state of the system.
#:
#: It is worse than a wrong number, because of the direction it fails. Raising an agent to
#: its own ceiling is the *desired* action and it lit the alarm; and an agent genuinely
#: above its ceiling was counted in the same eight as an unremarkable neighbour, so the one
#: row a reviewer must see is the one indistinguishable from the rest. The label said "above
#: ceiling" and the code meant "not equal to ceiling".
#:
#: Both columns are `varchar`, so the comparison is made on **rank**, not on the string.
#: `AUTONOMY_RANK`'s own comment in `persistence/process.py` says it: *"`L1 < L2` is false as
#: text and true as an integer, and getting that wrong fails open — which is the direction
#: that hurts."* `substring(... from 2)::int` reads the digit, which is total over the
#: vocabulary as long as every level is `L` and one digit;
#: `test_every_autonomy_level_is_one_letter_and_a_digit` in
#: `tests/unit/test_construction_schema.py` is what keeps that true.
#:
#: `tightest_ceiling` is the *least* autonomy any agent is allowed, so it is a minimum over
#: ranks. `min()` on the text happened to agree, for the same single-digit reason, and was
#: changed for the same reason: a coincidence that produces the right answer is not a
#: property anyone can rely on when a fifth level is added.
#:
#: The level is rebuilt as `'L' || <rank>` rather than selected from the rows, because
#: `ARRAY_AGG(DISTINCT autonomy_ceiling ORDER BY substring(autonomy_ceiling from 2)::int)`
#: is rejected by Postgres — *"`in an aggregate with DISTINCT, ORDER BY expressions must
#: appear in argument list`"* — and the alternative, a correlated subquery, would put a
#: second scan of `agents` inside the one query that exists to be a single pass.
_AGENT_POSTURE = """
SELECT count(*) AS agents,
       count(*) FILTER (WHERE kill_switch) AS killed,
       count(*) FILTER (WHERE NOT kill_switch
          AND substring(granted_level from 2)::int
              > substring(autonomy_ceiling from 2)::int) AS above_l1,
       'L' || min(substring(autonomy_ceiling from 2)::int)::text AS tightest_ceiling
FROM agents
WHERE organization_id = CAST(:o AS varchar(40))
"""


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    """One project in the CEO's view."""

    project_id: str
    code: str
    name: str
    status: str
    wbs_nodes: int
    gates_open: int
    gates_decided: int
    contract_value: float | None = None
    currency_code: str = ""
    planned_start: dt.date | None = None
    planned_completion: dt.date | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "code": self.code,
            "name": self.name,
            "status": self.status,
            "wbs_nodes": self.wbs_nodes,
            "gates_open": self.gates_open,
            "gates_decided": self.gates_decided,
            "contract_value": self.contract_value,
            "currency_code": self.currency_code,
            "planned_start": self.planned_start.isoformat() if self.planned_start else None,
            "planned_completion": (
                self.planned_completion.isoformat() if self.planned_completion else None
            ),
        }


@dataclass(frozen=True, slots=True)
class AgentPosture:
    """How much the organisation has delegated, in one line.

    `above_l1` is the number that matters and it is expected to be **zero** for a
    while. `0016` defaults every agent to L1, and nothing here raises a ceiling: the
    dossier's limits are the ceiling, and the measured shadow agreement rate is what
    should raise a granted level, and there are no shadow runs yet.

    Zero is the *expected* value, which makes it the easiest number in the product to get
    wrong. A figure that is wrong here is wrong in the reassuring direction — the tile
    reads 0 and nobody looks — so it is asserted against the database rather than trusted,
    and `make verify-page` fails when the served page disagrees with the rows. That check
    is what caught the first version of this, which reported 8.
    """

    agents: int
    killed: int
    above_l1: int
    tightest_ceiling: str = "L1"

    def as_dict(self) -> dict[str, object]:
        return {
            "agents": self.agents,
            "killed": self.killed,
            "above_l1": self.above_l1,
            "tightest_ceiling": self.tightest_ceiling,
        }


@dataclass(frozen=True, slots=True)
class Cockpit:
    projects: tuple[PortfolioRow, ...] = ()
    posture: AgentPosture | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "projects": [p.as_dict() for p in self.projects],
            "posture": self.posture.as_dict() if self.posture else None,
            "totals": {
                "projects": len(self.projects),
                "wbs_nodes": sum(p.wbs_nodes for p in self.projects),
                "gates_open": sum(p.gates_open for p in self.projects),
                # A sum, and `None` as zero: a project with no contract value
                # recorded contributes nothing, which is a different statement from
                # "this project is worth nothing" -- and a per-project breakdown in
                # `projects` is where that difference can be seen.
                "contract_value": sum(p.contract_value or 0 for p in self.projects),
            },
        }


async def ceo_cockpit(conn: ReadConnection, *, organization_id: str) -> Cockpit:
    """The portfolio, in the order a CEO would read it.

    **Does not report schedule slip.** It reports `wbs_nodes`, gates and value, and it
    leaves the slip figure to `pm_workspace`, because a portfolio-level slip number is
    an average over projects of wildly different sizes and an average that hides a
    project 40 days late behind four that are on time is worse than no number.
    """
    rows = (await conn.execute(text(_PORTFOLIO), {"o": organization_id})).mappings().all()
    post = (
        (await conn.execute(text(_AGENT_POSTURE), {"o": organization_id})).mappings().one_or_none()
    )
    return Cockpit(
        projects=tuple(
            PortfolioRow(
                project_id=r["id"],
                code=r["code"],
                name=r["name"],
                status=r["status"],
                wbs_nodes=r["wbs_nodes"] or 0,
                gates_open=r["gates_open"] or 0,
                gates_decided=r["gates_decided"] or 0,
                contract_value=(
                    float(r["contract_value"]) if r["contract_value"] is not None else None
                ),
                currency_code=r["currency_code"] or "",
                planned_start=r["planned_start"],
                planned_completion=r["planned_completion"],
            )
            for r in rows
        ),
        posture=AgentPosture(
            agents=post["agents"] or 0,
            killed=post["killed"] or 0,
            above_l1=post["above_l1"] or 0,
            tightest_ceiling=post["tightest_ceiling"] or "L1",
        )
        if post
        else None,
    )


# --- PM workspace --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZonePosition:
    """One work package's schedule position, and — crucially — whether it has one."""

    code: str
    name: str
    parent_code: str | None
    readings: int
    #: Readings that actually recorded an outcome. Zero means the file recorded a plan
    #: and no actual, which is *not* the same as being on time.
    measured: int
    last_planned_finish: dt.date | None = None
    last_actual_finish: dt.date | None = None
    slip_days: int | None = None

    @property
    def is_measured(self) -> bool:
        return self.measured > 0

    @property
    def is_late(self) -> bool:
        return self.slip_days is not None and self.slip_days > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "parent_code": self.parent_code,
            "readings": self.readings,
            "measured": self.measured,
            "is_measured": self.is_measured,
            "is_late": self.is_late,
            "slip_days": self.slip_days,
            "last_planned_finish": (
                self.last_planned_finish.isoformat() if self.last_planned_finish else None
            ),
            "last_actual_finish": (
                self.last_actual_finish.isoformat() if self.last_actual_finish else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Workspace:
    """One project's breakdown, its lateness, and what is simply unmeasured.

    `unmeasured` is a first-class list rather than a flag. A project where four zones
    are late and two have never been measured is a different situation from one where
    six zones are late, and an endpoint that returned only the first would let someone
    conclude the second.
    """

    project_id: str
    zones: tuple[ZonePosition, ...] = ()
    late: tuple[ZonePosition, ...] = ()
    unmeasured: tuple[ZonePosition, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "zones": [z.as_dict() for z in self.zones],
            "late": [z.as_dict() for z in self.late],
            "unmeasured": [z.as_dict() for z in self.unmeasured],
            "counts": {
                "zones": len(self.zones),
                "late": len(self.late),
                "unmeasured": len(self.unmeasured),
            },
        }


async def pm_workspace(conn: ReadConnection, *, organization_id: str, project_id: str) -> Workspace:
    """Every work package in a project, with its position and its lateness.

    Three lists, from two queries, and the split is the point:

    * `zones` — everything, in sequence order.
    * `late` — measured and finished after its planned day, worst first.
    * `unmeasured` — has a plan and no actual. **Not** counted as on time, which is
      what the real corpus would otherwise force: 100% of `TĐ BOH.xlsx`'s activities
      have empty actual columns, so a naive "variance = actual - planned" reports the
      whole project as perfectly on schedule.
    """
    rows = (
        (await conn.execute(text(_ZONE_POSITION), {"o": organization_id, "project": project_id}))
        .mappings()
        .all()
    )
    late_rows = (
        (await conn.execute(text(_LATE_ZONES), {"o": organization_id, "project": project_id}))
        .mappings()
        .all()
    )
    zones: list[ZonePosition] = []
    for r in rows:
        slip = r["slip_days"]
        zones.append(
            ZonePosition(
                code=r["code"],
                name=r["name"],
                parent_code=None,
                readings=r["readings"] or 0,
                measured=r["measured"] or 0,
                last_planned_finish=r["last_planned_finish"],
                last_actual_finish=r["last_actual_finish"],
                slip_days=int(slip) if slip is not None else None,
            )
        )
    by_code = {z.code: z for z in zones}
    return Workspace(
        project_id=project_id,
        zones=tuple(zones),
        late=tuple(
            ZonePosition(
                code=r["code"],
                name=r["name"],
                parent_code=None,
                readings=by_code[r["code"]].readings if r["code"] in by_code else 0,
                measured=by_code[r["code"]].measured if r["code"] in by_code else 0,
                slip_days=int(r["slip_days"]),
            )
            for r in late_rows
        ),
        unmeasured=tuple(z for z in zones if not z.is_measured),
    )


# --- HITL inbox ----------------------------------------------------------------

#: `expires_at` is selected because **without it the queue cannot tell a row that
#: can still be decided from one that cannot**, and it rendered an Approve button
#: on both. The operator clicked, the service refused, and the message --
#: "approval apr_… expired at 2026-09-29T15:46:06" -- was the only place the
#: expiry was visible at all.
#:
#: `t.title` is selected for the same class of reason. Two pending rows for
#: `hr.open_headcount`, on two different tasks, rendered as two identical lines
#: reading `hr.open_headcount · asked by <agent> · waiting 1d` -- and the operator
#: has no way to tell "the same question twice" from "two different questions".
#: They are two different tasks, each from its own run.
_INBOX = """
SELECT a.id, a.action_type, a.status, a.created_at, a.decided_at, a.expires_at,
       a.effect_class, a.risk_level, a.task_id, a.requested_by,
       a.requested_by_type, a.assigned_approver_id,
       t.title AS task_title
FROM approvals a
LEFT JOIN tasks t ON t.id = a.task_id AND t.organization_id = a.organization_id
WHERE a.organization_id = CAST(:o AS varchar(40)) AND a.status = 'pending'
ORDER BY a.created_at
"""


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One thing somebody has to decide.

    `waiting_since` is a `timedelta` and `waiting_days` a number, because the mobile
    surface shows "3 days" and the audit wants the instant. An approval nobody can rank
    by urgency is a queue people work from oldest-first by habit rather than by policy.
    """

    approval_id: str
    action_type: str
    effect_class: str
    risk_level: str
    created_at: dt.datetime
    task_id: str | None = None
    #: Free text rather than a `users` reference, because the actor has to survive
    #: their departure -- which is also why a null is reported rather than dropped.
    requested_by: str | None = None
    requested_by_type: str = ""
    #: Who this was routed to, if anywhere. Null means the queue, which on mobile is
    #: the common case and is worth saying out loud rather than rendering as nobody.
    assigned_approver_id: str | None = None
    decided_at: dt.datetime | None = None
    #: When this request stops being answerable. Carried rather than left to the
    #: reader to infer, because `decide()` refuses an expired row and a queue that
    #: offers an action the platform will reject is a queue that wastes a person's
    #: time to teach them the truth.
    expires_at: dt.datetime | None = None
    #: What this request is about, when the task row is still there. Two rows for
    #: the same action on different tasks are two questions, not one asked twice.
    task_title: str | None = None

    def waiting_for(self, now: dt.datetime) -> dt.timedelta:
        return max(now - self.created_at, dt.timedelta(0))

    def expired(self, now: dt.datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def as_dict(self, *, now: dt.datetime) -> dict[str, object]:
        waiting = self.waiting_for(now)
        return {
            "approval_id": self.approval_id,
            "action_type": self.action_type,
            "effect_class": self.effect_class,
            "risk_level": self.risk_level,
            "task_id": self.task_id,
            "requested_by": self.requested_by,
            "requested_by_type": self.requested_by_type,
            "assigned_approver_id": self.assigned_approver_id,
            "waiting_days": waiting.days,
            "waiting_seconds": int(waiting.total_seconds()),
            "task_title": self.task_title,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            # The single field the page needs and did not have. A row that is not
            # actionable must not render a button, and "not actionable" is a fact
            # about a deadline, not about a status someone forgot to update.
            "actionable": not self.expired(now),
            "expired": self.expired(now),
        }


async def hitl_inbox(
    conn: ReadConnection, *, organization_id: str, now: dt.datetime
) -> list[PendingApproval]:
    """Everything pending, oldest first.

    Excludes nothing. An approval with no requester is listed with no requester rather
    than dropped, because a queue that silently omits rows cannot be reconciled against
    the `approvals` table — and a person deciding on a queue they cannot count is
    deciding on a queue they do not trust.

    It also does **not** sort by urgency. The dossier's L1 ceiling is what governs who
    approves, and this is a list of what is waiting, not a recommendation about what to
    do next.

    `now` is a parameter for the reason it is one everywhere else: the purity test
    allows a clock read in exactly one file, and a wait computed against a per-row
    clock cannot be reproduced from the report.
    """
    if now.tzinfo is None:
        raise ValueError(
            "now must be timezone-aware; `approvals.created_at` is timestamptz so a "
            "naive value raises TypeError from inside the subtraction rather than here"
        )
    rows = (await conn.execute(text(_INBOX), {"o": organization_id})).mappings().all()
    return [
        PendingApproval(
            approval_id=r["id"],
            action_type=r["action_type"] or "",
            effect_class=r["effect_class"] or "",
            risk_level=r["risk_level"] or "",
            created_at=r["created_at"],
            task_id=r["task_id"],
            requested_by=r["requested_by"],
            requested_by_type=r["requested_by_type"] or "",
            assigned_approver_id=r["assigned_approver_id"],
            decided_at=r["decided_at"],
            expires_at=r["expires_at"],
            task_title=r["task_title"],
        )
        for r in rows
    ]


__all__ = [
    "AgentPosture",
    "Cockpit",
    "PendingApproval",
    "PortfolioRow",
    "Workspace",
    "ZonePosition",
    "ceo_cockpit",
    "hitl_inbox",
    "pm_workspace",
]
