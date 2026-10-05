"""Acting on the reaper's verdicts, and writing down what it did.

`domain/reaper.py` decides. This module fetches the candidates, hands each one to
`assess_task` with a single `now`, and performs the two automatic actions. It follows
`progress_operations.py` exactly: SQL is module constants, `now` is a required
parameter, and each statement casts `:o` because `organization_id` is `varchar(40)`
and an untyped bind makes Postgres fail with an `AmbiguousParameterError` that names
neither the column nor the cause.

## The whole design is one UPDATE's WHERE clause

`assess_task` reads a row and decides its lease has expired. Between that read and
the write, a worker can renew the lease — and if it has, the reaper is about to
`UPDATE` a task somebody is actively working on. The decision was correct when it was
made and is wrong by the time it is acted on.

So every write re-states the condition it acted on:

    WHERE id = :id AND status = 'running' AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= :now

`rowcount == 0` then means something specific and worth reporting: the task was
reclaimed, or a live worker beat the reaper to it. Those are different facts, and a
report that conflates them would claim a reclaim that never happened. They are
`reclaimed` and `contended` in `ReapReport`, and `TestTheRaceGuardFires` proves the
second path is reachable rather than theoretical.

This is the same reason `domain/reaper.py` refuses to reclaim a `running` task with
*no* lease: there the condition cannot be re-stated, because there is nothing to
re-state, and an unconditional write is exactly the race this clause exists to
prevent.

## The findings are not actions, and the audit log knows the difference

`audit_logs` gets one row per thing *done*, not per thing reported. A sweep that
surfaced forty overdue approvals did not do forty things; it did zero. Logging
findings as actions would make the audit trail claim the system made forty decisions
it only noticed — which is the failure mode the dossier's segregation-of-duties
principle is about, and the reason `assess_task` returns `SURFACE` rather than
deciding.

The report keeps both, and `as_dict()` exposes `surfaced` and `actioned` as separate
numbers, so a reader can tell the size of the problem from the amount the system did
about it.

## Idempotence, which comes free and is still tested

Both actions move a task to `queued`, and `assess_task` returns `None` for `queued`.
So a second sweep over the same rows finds nothing to do. That is not a coincidence
worth relying on silently: `TestASecondSweepChangesNothing` runs the whole thing
twice and asserts the second report is empty.

## The SQL filter carries no policy

The candidate query excludes recent rows, and the threshold it uses is *derived* from
the policy's smallest delay rather than written as a second literal. That matters
because a second copy of a threshold is a second thing to forget to change: someone
tightening `surface_after` to 4 hours would be baffled by a query still filtering at
48. The query is a load-size guard, not a decision — every row it returns is still
judged by `assess_task`, and a stricter policy can only ever *add* candidates here.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application.fleet_view import STUCK_AFTER_SECONDS
from ai_orchestrator.domain.enums import TaskStatus
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.reaper import (
    Action,
    ReapPolicy,
    TaskSnapshot,
    Verdict,
    assess_task,
    summarise,
)

#: The statuses the candidate query fetches, in Python, so they can be checked
#: against the domain rather than trusted. The query spells them out as a literal
#: instead of interpolating this tuple, because building SQL from a Python list
#: needs an `S608` suppression even when the list is a module constant, and a
#: suppression is an assertion in a comment where a test is a fact.
#: `TestTheQueryFetchesExactlyWhatTheDomainJudges` in the unit tests asserts the two
#: agree, so the literal and this tuple cannot drift apart unnoticed.
#:
#: What is left out, and why: the three that have not started (`created`, `queued`,
#: `assigned`) and the three that are finished (`completed`, `canceled`, `expired`).
#: A reaper that scanned those would be loading rows it has no rule about.
REAPABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_FOR_INPUT,
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.FAILED,
    }
)

_CANDIDATES = """
SELECT id, status, updated_at, lease_expires_at, attempt_count,
       failure_category, title
FROM tasks
WHERE organization_id = CAST(:o AS varchar(40))
  AND status IN ('running', 'blocked', 'waiting_for_input',
                 'waiting_for_approval', 'failed')
  AND (
        updated_at <= CAST(:floor AS timestamptz)
        OR lease_expires_at <= CAST(:now AS timestamptz)
      )
ORDER BY updated_at
LIMIT :limit
"""

#: The race guard, stated in SQL. `status` and `lease_expires_at` are re-checked
#: because the decision was made on a row that has since had time to change. The
#: `attempt_count` is returned so the caller can report *which attempt* was resumed
#: without a second query, and so a test can assert it did not move.
_RECLAIM = """
UPDATE tasks
SET status = 'queued',
    lease_expires_at = NULL,
    updated_at = CAST(:now AS timestamptz)
WHERE id = :id
  AND organization_id = CAST(:o AS varchar(40))
  AND status = 'running'
  AND lease_expires_at IS NOT NULL
  AND lease_expires_at <= CAST(:now AS timestamptz)
RETURNING attempt_count
"""

_REQUEUE = """
UPDATE tasks
SET status = 'queued',
    lease_expires_at = NULL,
    updated_at = CAST(:now AS timestamptz)
WHERE id = :id
  AND organization_id = CAST(:o AS varchar(40))
  AND status = 'failed'
RETURNING attempt_count
"""

_AUDIT = """
INSERT INTO audit_logs (
    id, organization_id, actor_id, actor_type, action, resource_type,
    resource_id, task_id, outcome, policy_decision, policy_reason, context, created_at
)
VALUES (
    :id, CAST(:o AS varchar(40)), NULL, 'system:task_reaper', :action, 'task',
    :task, :task, :outcome, :decision, :reason, CAST(:context AS jsonb),
    CAST(:now AS timestamptz)
)
"""


@dataclass(frozen=True, slots=True)
class ReapReport:
    """What one sweep found, and what it did about it.

    `findings` keeps every verdict including the acted-on ones, so the report can be
    reconciled against `summarise(findings)` without re-running the sweep. The
    three action tuples partition the *actionable* findings, and
    `actioned == len(reclaimed) + len(requeued)` always holds.
    """

    organization_id: str
    swept_at: dt.datetime
    #: How many rows the candidate query returned. Not the same as "how many were
    #: problems" -- most sweeps load rows that need nothing.
    candidates: int = 0
    findings: tuple[Verdict, ...] = field(default_factory=tuple)
    reclaimed: tuple[str, ...] = field(default_factory=tuple)
    requeued: tuple[str, ...] = field(default_factory=tuple)
    #: Tasks the reaper decided to act on and could not, because the row no longer
    #: matched the condition it judged. A live worker renewed the lease between the
    #: read and the write. Reported rather than swallowed, because "I could not take
    #: this task" is information and "nothing happened" is not.
    contended: tuple[str, ...] = field(default_factory=tuple)
    policy: ReapPolicy = field(default_factory=ReapPolicy)

    @property
    def surfaced(self) -> tuple[Verdict, ...]:
        """The findings that were reported and not acted on."""
        return tuple(v for v in self.findings if v.action is Action.SURFACE)

    @property
    def actioned(self) -> int:
        return len(self.reclaimed) + len(self.requeued)

    def as_dict(self) -> dict[str, object]:
        """The report as an operations summary.

        `surfaced` and `actioned` are separate keys on purpose. A sweep reporting
        "40" is ambiguous between "40 things need a person" and "40 things the system
        did", and those two call for completely different responses.
        """
        return {
            "organization_id": self.organization_id,
            "swept_at": self.swept_at.isoformat(),
            "candidates": self.candidates,
            "surfaced": len(self.surfaced),
            "actioned": self.actioned,
            "reclaimed": list(self.reclaimed),
            "requeued": list(self.requeued),
            "contended": list(self.contended),
            **summarise(list(self.findings)),
        }


#: Executions stranded on a task that has since reached a terminal state.
#:
#: **This condition does not use the lease, and that is the point.** The reaper's own
#: condition is lease-based, and the lease is written inside the run's transaction and never
#: committed before the run ends (F190), so no other connection can see it while the run is in
#: flight. The consequence is not theoretical: in the development tenant, **8 executions have
#: been marked `running` since 27 September on tasks that reached `failed`**, and
#: `local_runner`'s double-run guard counts exactly these -- so pressing Run answers
#: *"1 execution(s) are already running for this task, so something else is working on it"*
#: and will answer the same thing **forever**, about work nobody is doing.
#:
#: A message that says someone is on it, when nobody ever will be, is worse than no message.
#: It is the kind of lie an operator cannot act on, because acting on it means waiting.
#:
#: The condition is a **fact, not a timeout**. A task in `completed`, `failed`, `cancelled` or
#: `blocked` cannot have a run legitimately in flight: the run is what moved the task there.
#: So no clock is consulted and nothing is guessed -- which is why this is a separate
#: statement rather than a shorter `lease_expires_at` threshold. A timeout would eventually
#: clear these too, and would also clear a run that is genuinely working. This cannot do
#: either.
_STALE_EXECUTIONS = """
UPDATE executions x
SET status = 'failed',
    error_message = :reason,
    -- The vocabulary the column already holds. `worker_lost` was invented here and the table
    -- carries no CHECK to catch it, which is exactly how a fourth spelling of "why a run
    -- stopped" becomes normal. Measured values in this tenant: `model_error`,
    -- `runtime_unavailable`, `budget_exhausted`. This is the fourth kind of thing, so it
    -- gets a fourth name -- but it is the executor's own taxonomy, not this file's.
    error_kind = 'runtime_unavailable',
    error_category = 'infrastructure',
    finished_at = :now
WHERE x.id IN (
    SELECT e.id
    FROM executions e
    JOIN tasks t ON t.id = e.task_id AND t.organization_id = e.organization_id
    WHERE e.organization_id = CAST(:o AS varchar(64))
      AND e.status = 'running'
      AND t.status IN ('completed', 'failed', 'cancelled', 'blocked')
)
RETURNING x.task_id
"""


#: Executions that started long ago on a task that never moved since.
#:
#: The gap `_STALE_EXECUTIONS` and `_ABANDONED_TASKS` leave between them: a task
#: still `running` with an execution still `running`, both untouched for a day.
#: The execution cannot be genuinely working — no run this platform has ever
#: measured lasted even an hour — and the task cannot be reaped while the
#: execution claims it, nor abandoned while the execution started inside the
#: window. Six identical complaint tasks sat `running` for a day on the live
#: tenant in exactly this state, and the register showed them as work in flight.
#:
#: The window is deliberately wider than the stranded-box window: six hours,
#: against a longest observed free-model run of ~22 minutes. Closing a run
#: that is genuinely working throws away up to 67,000 tokens of work, so the
#: margin is measured in multiples of the worst case, not in minutes past it.
#: A live run started inside the window is untouched, whatever the task says.
#:
#: This closes the *execution* only, like its terminal-state sibling: the task
#: stays `running` with no live execution, which is precisely the shape
#: `_ABANDONED_TASKS` already fails as infrastructure on the same sweep. Two
#: steps converging, each safe and idempotent on its own.
STALE_EXECUTION_AFTER_SECONDS = 6 * 3_600

_STALE_RUNNING_EXECUTIONS = """
UPDATE executions e
SET status = 'failed',
    error_message = :reason,
    error_kind = 'runtime_unavailable',
    error_category = 'infrastructure',
    finished_at = :now
WHERE e.id IN (
    SELECT x.id
    FROM executions x
    JOIN tasks t ON t.id = x.task_id AND t.organization_id = x.organization_id
    WHERE x.organization_id = CAST(:o AS varchar(64))
      AND x.status = 'running'
      AND x.started_at < :since
      AND t.status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
      AND t.updated_at < :since
)
RETURNING e.task_id
"""

STALE_RUNNING_EXECUTION_REASON = (
    "the run started long ago and neither it nor its task has moved since: no worker "
    "is working on it. Closed by the sweep rather than left to make the platform claim "
    "somebody is working on it; the task itself is failed separately as infrastructure."
)

#: A task nobody is holding and nobody will hold.
#:
#: `created` / `assigned` / `running`, older than the window, with **no** execution
#: started inside it. `running` is in the list and `running` is the interesting case: the
#: task claims to be being worked on, and nothing is working on it.
#:
#: `:since` rather than an interval literal, so the window is a parameter of the sweep and
#: a caller can say "an hour" without this module owning the number.
_ABANDONED_TASKS = """
UPDATE tasks t
SET status = 'failed',
    last_error = :reason,
    failure_category = 'infrastructure',
    updated_at = :now
WHERE t.id IN (
    SELECT t.id
    FROM tasks t
    WHERE t.organization_id = CAST(:o AS varchar(64))
      AND t.status IN ('created', 'assigned', 'running')
      AND t.updated_at < :since
      AND NOT EXISTS (
            SELECT 1
            FROM executions e
            WHERE e.task_id = t.id
              AND e.organization_id = t.organization_id
              AND e.status = 'running'
              AND e.started_at > :since
      )
)
RETURNING t.id
"""

#: The words a task fails with when nobody claimed it.
#:
#: Written for the **agent**, not for the operator, because this string is what the next
#: run reads. "no worker was available" is a fact about the platform's scheduling and tells
#: a model nothing; "nobody picked this up, so it never ran" is the thing worth learning
#: -- that the shape of the work, not the budget, kept it from being started.
ABANDONED_REASON = (
    "nobody picked this up, so it never ran. The task was assigned and left: no worker "
    "started an execution for it within the queue window, so there is no work to show "
    "and no result to check. This is a queue fault, not a decision by this department."
)


async def abandon_unclaimed_tasks(
    conn: AsyncConnection,
    *,
    organization_id: str,
    now: dt.datetime,
    older_than_s: int = STUCK_AFTER_SECONDS,
) -> list[str]:
    """Fail tasks nobody has claimed, and return their ids.

    ## Why this exists

    Measured on the development tenant, 2026-10-03: **106 tasks `assigned`, 0 `running`,
    and a person pressing Run with nothing happening.** The queue did not drain because
    nothing ever drained it. A task created over HTTP is committed and then, with no
    Temporal server and no worker, nothing ever claims it: it sits `assigned` for ever,
    it counts in "waiting on you", and it counts in nothing else. Every number that
    describes the organisation's work was reading a backlog of orphans.

    ## Why `failed`, and why now

    The alternative is to leave them, which is the state that was measured. The other
    alternative is to leave them `assigned` but hide them, which is the same lie with an
    extra step.

    `failed` is terminal and the state machine says so on purpose, so this needs the
    window to be wide enough that a task nobody claimed has certainly not been claimed
    late: the default is an hour, the same `STUCK_AFTER_SECONDS` the fleet view already
    uses to draw a stranded box. **One window, two consumers** -- so "the console shows a
    stranded run" and "the sweep gave up on it" can never disagree.

    Safe to run twice: the `UPDATE` matches only non-terminal rows, so a second sweep
    finds nothing. That is a consequence of the predicate rather than a coincidence, and
    it is what makes it safe to run on every `make page`.

    The reason is a sentence a department can learn from, because `procedure.recorded`
    reads failed tasks: the lesson worth keeping is "this shape of work does not get
    picked up", not "a worker was busy".
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "abandon_unclaimed_tasks(now=...) must carry a timezone offset; tasks."
            "updated_at is timestamptz and an untyped bind is ambiguous."
        )
    if older_than_s < 0:
        raise ValueError(f"older_than_s must be >= 0, got {older_than_s}")
    since = now - dt.timedelta(seconds=older_than_s)
    result = await conn.execute(
        text(_ABANDONED_TASKS),
        {"o": organization_id, "since": since, "now": now, "reason": ABANDONED_REASON},
    )
    failed = [str(row[0]) for row in result.fetchall()]

    # **One audit row per task actually failed.** Not one per finding -- a sweep that
    # changed nothing wrote nothing, which is the rule `_audit`'s docstring already
    # states and the reason a maintenance step is believed.
    #
    # `policy_decision = 'abandon'` is a fourth value alongside the verdict enum's own,
    # and it is worth being explicit about why the column has no CHECK: `action` is
    # free text and `policy_decision` already carries values from more than one writer
    # (`_STALE_EXECUTIONS` writes `runtime_unavailable` into an *error_kind*, and
    # `reap()` writes `domain.reaper` decisions into this one). Constraining the
    # vocabulary now would mean inventing a taxonomy across three writers to satisfy a
    # check, which is the kind of lock that stops the next fact being recorded at all.
    for task_id in failed:
        await conn.execute(
            text(_AUDIT),
            {
                "id": f"aud_{new_ulid()}",
                "o": organization_id,
                "action": "task.abandon",
                "task": task_id,
                "outcome": "failed",
                "decision": "abandon",
                "reason": ABANDONED_REASON,
                "context": json.dumps(
                    {
                        "unclaimed_for_seconds": older_than_s,
                        "window_source": "fleet_view.STUCK_AFTER_SECONDS",
                    },
                    ensure_ascii=False,
                ),
                "now": now,
            },
        )
    return failed


async def reap_stranded_executions(
    conn: AsyncConnection,
    *,
    organization_id: str,
    now: dt.datetime,
) -> list[str]:
    """Close executions stranded on a terminal task, and return the task ids.

    Not a verdict and not an action taken on a **task** -- the task is left exactly as it is,
    because `failed` is terminal and the state machine says nothing leaves it. What is
    corrected is the *execution* row, which is a fact about a run that cannot still be
    happening.

    Safe to run at any time and safe to run twice: the `WHERE` only matches rows still marked
    running on a terminal task, so a second sweep finds nothing. That is what makes it fit in
    `make page` rather than a scheduler nobody is running.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"reap_stranded_executions(now=...) must be timezone-aware; got {now!r}. "
            "The executions table stores timestamptz."
        )
    result = await conn.execute(
        text(_STALE_EXECUTIONS),
        {
            "o": organization_id,
            "now": now,
            "reason": (
                "the run's task reached a terminal state, so this execution cannot still be "
                "in flight; closed by the stranded-execution sweep rather than left to make "
                "the platform claim somebody is working on it"
            ),
        },
    )
    return [row[0] for row in result.fetchall()]


async def fail_stale_running_executions(
    conn: AsyncConnection,
    *,
    organization_id: str,
    now: dt.datetime,
    older_than_s: int = STALE_EXECUTION_AFTER_SECONDS,
) -> list[str]:
    """Close runs that started long ago on a task that never moved, and return task ids.

    The sibling above needs a terminal task; this one needs old age on *both* sides.
    A run that started inside the window is untouched whatever the task says, and a
    task that moved inside the window keeps its run — a live long run updates
    neither condition, so neither condition alone would be safe to act on.

    Safe to run twice: the `UPDATE` matches only `running` rows, so a second sweep
    finds nothing. Run before `abandon_unclaimed_tasks` on the same sweep: a task
    left `running` with no live execution is precisely what that step fails as
    infrastructure, so one sweep converges instead of two.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "fail_stale_running_executions(now=...) must be timezone-aware; "
            "executions.started_at is timestamptz."
        )
    if older_than_s < 0:
        raise ValueError(f"older_than_s must be >= 0, got {older_than_s}")
    since = now - dt.timedelta(seconds=older_than_s)
    result = await conn.execute(
        text(_STALE_RUNNING_EXECUTIONS),
        {
            "o": organization_id,
            "now": now,
            "since": since,
            "reason": STALE_RUNNING_EXECUTION_REASON,
        },
    )
    return [row[0] for row in result.fetchall()]


async def reap(
    conn: AsyncConnection,
    *,
    organization_id: str,
    now: dt.datetime,
    policy: ReapPolicy | None = None,
    limit: int = 500,
) -> ReapReport:
    """Sweep one tenant's tasks, act where the rules allow, and record what happened.

    `now` is required rather than defaulted, for the same reason `write_reading`
    requires `observed_on`: the domain purity test allows a clock read in exactly one
    file, and a sweep that decided "this is 49 hours old" against a clock read per
    row would produce verdicts that cannot be reproduced from the report.

    **`now` must be timezone-aware, and a naive one is refused here.** `tasks.updated_at`
    and `lease_expires_at` are `timestamptz`, so every row this reads comes back
    aware. Subtracting a naive `now` from one raises `TypeError: can't subtract
    offset-naive and offset-aware datetimes` from inside `_age` — a message that
    names neither the cause nor the fix, some way below the caller that caused it.
    Assumed-UTC instead would be worse: a sweep silently comparing a UTC instant
    against a row written in `+07:00` is off by seven hours, which on a two-hour
    lease margin is the difference between reclaiming a live task and not. So the
    boundary refuses, and says which column type the caller has to agree with.

    `limit` bounds the candidate set. A tenant with a million tasks gets a thousand at
    a time, oldest first, and the next sweep continues where this one stopped --
    `ORDER BY updated_at` makes the walk stable rather than a lottery.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"reap(now=...) must be timezone-aware; got a naive {now!r}. The "
            f"tasks table stores timestamptz, so every row this sweep compares "
            f"against is aware. Use dt.datetime.now(tz=dt.UTC) or your scheduler's "
            f"aware clock -- assuming UTC silently here would be wrong by the "
            f"caller's offset, which for a two-hour lease margin is not a rounding "
            f"error."
        )

    p = policy or ReapPolicy()

    # Derived from the policy's shortest delay rather than written out, so there is
    # exactly one place a threshold lives. One sweep of slack below it, so a task
    # exactly on the boundary is not filtered out by a query and then found by the
    # domain to be one second too young.
    floor = now - p.requeue_after - p.requeue_after

    result = await conn.execute(
        text(_CANDIDATES),
        {
            "o": organization_id,
            "now": now,
            "floor": floor,
            "limit": limit,
        },
    )
    rows = result.mappings().all()

    findings: list[Verdict] = []
    reclaimed: list[str] = []
    requeued: list[str] = []
    contended: list[str] = []

    # One savepoint for the whole sweep, not one per task. A sweep that fails on its
    # 200th task should not leave 199 tasks moved, because "which ones moved" is
    # then only answerable by re-running the sweep and comparing. All or nothing,
    # and the whole thing is a single repeatable unit.
    async with conn.begin_nested():
        for row in rows:
            verdict = assess_task(
                TaskSnapshot(
                    task_id=row["id"],
                    status=TaskStatus(row["status"]),
                    updated_at=row["updated_at"],
                    lease_expires_at=row["lease_expires_at"],
                    attempt_count=row["attempt_count"] or 0,
                    failure_category=row["failure_category"],
                    title=row["title"] or "",
                ),
                now,
                p,
            )
            if verdict is None:
                continue
            findings.append(verdict)

            if verdict.action is Action.RECLAIM:
                took = await _act(_RECLAIM, conn, organization_id, verdict.task_id, now)
                (reclaimed if took else contended).append(verdict.task_id)
                await _audit(
                    conn,
                    organization_id=organization_id,
                    verdict=verdict,
                    now=now,
                    outcome="reclaimed" if took else "contended",
                )
            elif verdict.action is Action.REQUEUE:
                took = await _act(_REQUEUE, conn, organization_id, verdict.task_id, now)
                requeued.append(verdict.task_id)
                await _audit(
                    conn,
                    organization_id=organization_id,
                    verdict=verdict,
                    now=now,
                    outcome="requeued" if took else "already_queued",
                )

    return ReapReport(
        organization_id=organization_id,
        swept_at=now,
        candidates=len(rows),
        findings=tuple(findings),
        reclaimed=tuple(reclaimed),
        requeued=tuple(requeued),
        contended=tuple(contended),
        policy=p,
    )


async def _act(
    sql: str,
    conn: AsyncConnection,
    organization_id: str,
    task_id: str,
    now: dt.datetime,
) -> bool:
    """Run one guarded action. `False` means the guard rejected it.

    `rowcount` rather than `RETURNING` because the question is binary: did the WHERE
    clause still hold? A `RETURNING` scan on an update that matched nothing is
    indistinguishable from one that matched, and a reaper cannot afford to guess.
    """
    outcome = await conn.execute(text(sql), {"id": task_id, "o": organization_id, "now": now})
    return outcome.rowcount == 1


async def _audit(
    conn: AsyncConnection,
    *,
    organization_id: str,
    verdict: Verdict,
    now: dt.datetime,
    outcome: str,
) -> None:
    """Record one thing the reaper did, and why.

    Not one row per finding. `audit_logs` is a record of actions; a sweep that
    surfaced forty overdue approvals performed zero actions, and writing forty audit
    rows would make the trail claim forty decisions that were never taken.

    `context` carries the reason and the age, because a row reading
    `action=reclaim status=queued` six weeks from now is not a record of anything
    without them.
    """
    await conn.execute(
        text(_AUDIT),
        {
            "id": f"aud_{new_ulid()}",
            "o": organization_id,
            "action": f"task.{verdict.action.value}",
            "task": verdict.task_id,
            "outcome": outcome,
            "decision": verdict.reason.value,
            "reason": verdict.detail,
            # A string, not a dict. asyncpg's jsonb encoder calls `.encode()` on
            # whatever it is handed, so a dict fails with `AttributeError: 'dict'
            # object has no attribute 'encode'` -- an error about a missing method
            # on a built-in, from a statement that never mentions JSON.
            # `progress_operations._json` is the same two lines; it is private to
            # that module and duplicating two lines beats a cross-module import of
            # something with an underscore.
            "context": json.dumps(
                {
                    "passive_for_seconds": (
                        int(verdict.passive_for.total_seconds())
                        if verdict.passive_for is not None
                        else None
                    ),
                    "actionable": verdict.is_actionable,
                },
                ensure_ascii=False,
                default=str,
            ),
            "now": now,
        },
    )


__all__ = [
    "ABANDONED_REASON",
    "REAPABLE_STATUSES",
    "STALE_EXECUTION_AFTER_SECONDS",
    "ReapReport",
    "abandon_unclaimed_tasks",
    "fail_stale_running_executions",
    "reap",
    "reap_stranded_executions",
]
