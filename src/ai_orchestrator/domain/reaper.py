"""Deciding what to do about a task that has stopped making progress.

Pure rules, no database and no clock — the split `domain/gates.py` exists to
enforce, and the purity test allows `datetime.now()` in exactly one file in the
repository. Everything here takes `now` as a parameter.

## Why this module exists

`domain/state_machines.py` defines `PASSIVE_TASK_STATUSES` and says, in a comment:

    States in which no progress happens unless an external event arrives. A task
    stuck in one of these needs a timer; see `docs/OPERATIONS.md` stuck-task sweeps.

`docs/OPERATIONS.md` §5.2 then answers with a `SELECT`. And there is no timer
anywhere: no sweeper, no reaper, no worker loop that looks. The `tasks` table
carries `lease_expires_at`, `attempt_count`, `failure_category`, `deadline_at` and
`last_error`, all of them written and none of them read.

So the platform could tell you a task was running, and could tell you a worker had
crashed, and could not tell you either. A task in `running` whose worker died stays
in `running` for ever, and a task waiting for a human nobody has heard from is
indistinguishable from one waiting on a human who is answering.

## The design principle, which is the opposite of the obvious one

**The default is to surface, not to act.** A reaper that changes things is a machine
that quietly cancels people's work, and the states it would be tempted to act on are
exactly the ones where a *person* owes something.

Only two actions are taken automatically, and both are cases where a contract that
already exists has expired:

* **`RECLAIM`** a `running` task whose `lease_expires_at` has passed. Whoever held
  the lease promised to renew it by then. Past that moment nobody is working on the
  task, and this is the one case where leaving it is certainly wrong.
* **`REQUEUE`** a `failed` task that is retryable and has attempts left, bounded by
  `attempt_count` against the policy's ceiling. A transient model timeout should not
  need a human.

Everything else becomes a `SURFACE` finding. A `waiting_for_approval` older than any
threshold is escalated, **never expired** — a pending approval is a person owing a
commercial decision, and a system that cancels it because it is old has made that
decision for them. `approvals.expires_at` exists for the cases that genuinely should
expire, and that is a different question with a different owner.

## `None` means fine

`assess_task` returns `None` for a task that needs nothing. Most tasks need nothing,
most of the time, and a reaper that reports every task as a finding is a reaper
whose findings are ignored — which is the failure mode `TestIdentifiersFitPostgres`
and half the rest of this repository exist to avoid.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from ai_orchestrator.domain.enums import TaskStatus

#: Hours. One policy object, so the thresholds are a decision in one place rather
#: than a literal in six branches. The numbers are not derived from anything — the
#: corpus says nothing about how long a Vietnamese construction project takes to
#: answer an email — and they are deliberately conservative, because the cost of
#: surfacing something too early is a notification and the cost of expiring
#: something too early is somebody's approval disappearing.
DEFAULT_SURFACE_AFTER_HOURS = 48
DEFAULT_REQUEUE_AFTER_MINUTES = 5
DEFAULT_LEASE_HOURS = 2
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ReapPolicy:
    """When a passive task becomes a finding, and what may be done about it.

    `max_attempts` is separate from `tasks.attempt_count` on purpose: the column is
    what has happened, this is what may happen, and conflating them means a policy
    change silently rewrites history.
    """

    #: How long before a task waiting on a person becomes a *finding*. Long,
    #: because the cost of surfacing early is a notification.
    surface_after: dt.timedelta = dt.timedelta(hours=DEFAULT_SURFACE_AFTER_HOURS)
    #: How long before a retryable failure is *retried*. Short, and deliberately a
    #: different number from `surface_after`: a transient model timeout should be
    #: retried in minutes, not in two days, and using one threshold for both is what
    #: made the first version of `_assess_failed` either requeue a task a second
    #: after it failed or hold a recoverable failure for 48 hours. One governs when to
    #: *mention* something to a person, the other when a machine should *act*, and
    #: conflating them means one of the two is always wrong.
    requeue_after: dt.timedelta = dt.timedelta(minutes=DEFAULT_REQUEUE_AFTER_MINUTES)
    lease_duration: dt.timedelta = dt.timedelta(hours=DEFAULT_LEASE_HOURS)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: Set false to make the reaper read-only. Useful for measuring how much it
    #: *would* do before letting it do anything, which is the same shadow-mode
    #: discipline the dossier applies to its agents.
    allow_reclaim: bool = True
    allow_requeue: bool = True


class Action(StrEnum):
    """What the reaper decided to do."""

    NONE = "none"
    #: A `running` task whose lease expired. Safe to act on: the lease is a promise
    #: that has been broken, and the task cannot progress without one.
    RECLAIM = "reclaim"
    #: A retryable failure with attempts left.
    REQUEUE = "requeue"
    #: Report it and change nothing. The default for anything a person owes.
    SURFACE = "surface"


class Reason(StrEnum):
    """Why, in words a person can act on.

    Named rather than numbered because the output goes into an operations report and
    somebody reads it at 07:00. `STUCK_LEASE_EXPIRED` is actionable;
    `REASON_3` is a lookup.
    """

    LEASE_EXPIRED = "lease_expired"
    NO_LEASE = "no_lease_on_a_running_task"
    WAITING_FOR_APPROVAL_TOO_LONG = "waiting_for_approval"
    WAITING_FOR_INPUT_TOO_LONG = "waiting_for_input"
    BLOCKED_TOO_LONG = "blocked"
    RETRYABLE_FAILURE = "retryable_failure"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    FAILURE_NOT_RETRYABLE = "not_retryable"


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """The columns the reaper reads, and nothing else.

    A deliberate narrow surface. A rule that could reach `tasks.last_error` would
    one day start branching on an error string, and a reaper whose behaviour depends
    on prose is a reaper that changes when someone rewords a log line.
    """

    task_id: str
    status: TaskStatus
    updated_at: dt.datetime
    lease_expires_at: dt.datetime | None = None
    attempt_count: int = 0
    failure_category: str | None = None
    title: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    """A decision, and the sentence explaining it."""

    action: Action
    reason: Reason
    task_id: str
    #: How long the task has been in its current state. `None` when the answer is
    #: "immediately", which is the case for an expired lease.
    passive_for: dt.timedelta | None = None
    detail: str = ""

    @property
    def is_actionable(self) -> bool:
        """Whether the reaper may do this without a person deciding first.

        Only the two actions backed by an already-expired contract. Everything else
        is a finding.
        """
        return self.action in (Action.RECLAIM, Action.REQUEUE)


#: Failure categories that are worth another attempt. Everything else is a real
#: failure and retrying it just burns money — Tập 1's tiering exists for this.
RETRYABLE_FAILURES = frozenset(
    {
        "timeout",
        "rate_limited",
        "model_unavailable",
        "transient",
        "tool_error",
        "internal",
    }
)


def _age(updated_at: dt.datetime, now: dt.datetime) -> dt.timedelta:
    """How long since the task last moved.

    Clamped at zero. A task updated "in the future" is a clock skew or a timezone
    bug, and reporting it as `-3 days passive` would put a nonsense number in an
    operations report and sort it to the top of the wrong list.
    """
    return max(now - updated_at, dt.timedelta(0))


def assess_task(
    task: TaskSnapshot, now: dt.datetime, policy: ReapPolicy | None = None
) -> Verdict | None:
    """What, if anything, to do about one task. `None` means it needs nothing.

    `now` is a parameter rather than a clock read so the whole rule set is testable
    without freezing time, and so a caller sweeping a batch uses one `now` for every
    task rather than a slightly different one per row.
    """
    p = policy or ReapPolicy()

    # Terminal states first, and unconditionally. A reaper that touches a completed
    # task is a bug that looks like data loss, and there is no version of this
    # function where `completed` is anything but `None`.
    if task.status in (
        TaskStatus.COMPLETED,
        TaskStatus.CANCELED,
        TaskStatus.EXPIRED,
    ):
        return None

    if task.status is TaskStatus.RUNNING:
        return _assess_running(task, now, p)

    if task.status in (
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.WAITING_FOR_INPUT,
        TaskStatus.BLOCKED,
    ):
        return _assess_passive(task, now, p)

    if task.status is TaskStatus.FAILED:
        return _assess_failed(task, now, p)

    # `created`, `queued`, `assigned`: not yet running, so nothing is owed and
    # nothing has gone wrong. `None`.
    return None


def _assess_running(task: TaskSnapshot, now: dt.datetime, p: ReapPolicy) -> Verdict | None:
    """A running task is stuck if the lease has expired, and suspicious without one.

    The distinction is the whole of this function. A lease is a promise to renew by
    a time; an expired one has been broken and the task cannot progress. *No* lease
    on a running task means the code path that started it does not take one, which is
    a bug — but the task may be genuinely in flight, so it is surfaced rather than
    reclaimed. Reclaiming a live task races the worker doing the work.
    """
    if task.lease_expires_at is None:
        age = _age(task.updated_at, now)
        if age < p.surface_after:
            return None
        return Verdict(
            action=Action.SURFACE,
            reason=Reason.NO_LEASE,
            task_id=task.task_id,
            passive_for=age,
            detail=(
                f"running for {age} with no lease, so nothing is watching it. Not "
                f"reclaimed: it may genuinely be in flight, and a reaper that races "
                f"a live worker is worse than one that reports."
            ),
        )
    if task.lease_expires_at > now:
        return None
    return Verdict(
        action=Action.RECLAIM if p.allow_reclaim else Action.SURFACE,
        reason=Reason.LEASE_EXPIRED,
        task_id=task.task_id,
        passive_for=_age(task.updated_at, now),
        detail=(
            f"lease expired {_age(task.lease_expires_at, now)} ago; the holder "
            f"promised to renew by then and did not, so nothing is working on this"
        ),
    )


#: Which passive status surfaces as which reason. A table rather than three
#: near-identical branches, because the three differ only in the sentence a person
#: reads and that sentence is the output.
_PASSIVE_REASONS: dict[TaskStatus, Reason] = {
    TaskStatus.WAITING_FOR_APPROVAL: Reason.WAITING_FOR_APPROVAL_TOO_LONG,
    TaskStatus.WAITING_FOR_INPUT: Reason.WAITING_FOR_INPUT_TOO_LONG,
    TaskStatus.BLOCKED: Reason.BLOCKED_TOO_LONG,
}

#: What a person is owed, per passive status. Used in the detail sentence, and the
#: reason these are never expired is written out rather than implied.
_PASSIVE_OWES: dict[TaskStatus, str] = {
    TaskStatus.WAITING_FOR_APPROVAL: "a decision somebody has to make",
    TaskStatus.WAITING_FOR_INPUT: "an answer somebody has to give",
    TaskStatus.BLOCKED: "an unblocking action, and the reason it has not happened",
}


def _assess_passive(task: TaskSnapshot, now: dt.datetime, p: ReapPolicy) -> Verdict | None:
    """A task waiting on a person is surfaced, and never expired.

    The threshold decides *when to mention it*, not *what to do about it*. A pending
    approval is a person owing a commercial decision; cancelling their queue item
    because it is old is the system making that decision for them, and the dossier's
    segregation-of-duties principle is exactly about not doing that silently.
    `approvals.expires_at` is where a genuine expiry lives, and that is a different
    question with a different owner.
    """
    age = _age(task.updated_at, now)
    if age < p.surface_after:
        return None
    reason = _PASSIVE_REASONS[task.status]
    return Verdict(
        action=Action.SURFACE,
        reason=reason,
        task_id=task.task_id,
        passive_for=age,
        detail=(
            f"{task.status.value} for {age}, waiting on "
            f"{_PASSIVE_OWES[task.status]}. Surfaced, not expired: expiring it "
            f"would be the system taking that decision."
        ),
    )


def _assess_failed(task: TaskSnapshot, now: dt.datetime, p: ReapPolicy) -> Verdict | None:
    """A failure is either worth retrying, exhausted, or neither.

    This is the case where "ensuring the work gets done" is an action rather than a
    report: a model timeout that killed a task three seconds before it would have
    succeeded should not need a human. It is also the case where an unbounded retry
    is expensive, so `attempt_count` is compared against `policy.max_attempts` and
    the boundary is tested on both sides.

    The three outcomes are gated on **different** thresholds, which is the part worth
    reading twice. A retryable failure is gated on `requeue_after` -- minutes -- so a
    transient error recovers quickly. The two *reporting* outcomes are gated on
    `surface_after` -- hours -- so a permanent failure is not announced every sweep.
    The first version gated all three on `surface_after` and requeued a task a second
    after it failed, which both races the operator already looking at it and burns an
    attempt from the ceiling on a failure nothing has had time to recover from.
    """
    category = (task.failure_category or "").strip().lower()
    retryable = category in RETRYABLE_FAILURES
    age = _age(task.updated_at, now)

    if not retryable:
        if age < p.surface_after:
            return None
        return Verdict(
            action=Action.SURFACE,
            reason=Reason.FAILURE_NOT_RETRYABLE,
            task_id=task.task_id,
            passive_for=age,
            detail=(
                f"failed with {category or 'no category'}, which is not a transient "
                f"category, so another attempt would spend money to fail the same way"
            ),
        )

    if task.attempt_count >= p.max_attempts:
        if age < p.surface_after:
            return None
        return Verdict(
            action=Action.SURFACE,
            reason=Reason.ATTEMPTS_EXHAUSTED,
            task_id=task.task_id,
            passive_for=age,
            detail=(
                f"failed {task.attempt_count} time(s) and the ceiling is "
                f"{p.max_attempts}; a person decides whether to try again"
            ),
        )

    if age < p.requeue_after:
        return None
    return Verdict(
        action=Action.REQUEUE if p.allow_requeue else Action.SURFACE,
        reason=Reason.RETRYABLE_FAILURE,
        task_id=task.task_id,
        passive_for=age,
        detail=(f"failed with {category} on attempt {task.attempt_count} of {p.max_attempts}"),
    )


def summarise(verdicts: list[Verdict]) -> dict[str, int]:
    """Counts by action and by reason, for the operations report.

    By *both*, deliberately. "12 findings" sends somebody to the table; "12 findings,
    2 of them expired leases" sends them to the two tasks that are certainly stuck.
    """
    by_action: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for v in verdicts:
        by_action[v.action.value] = by_action.get(v.action.value, 0) + 1
        by_reason[v.reason.value] = by_reason.get(v.reason.value, 0) + 1
    return {
        "total": len(verdicts),
        "actionable": sum(1 for v in verdicts if v.is_actionable),
        **{f"action_{k}": n for k, n in sorted(by_action.items())},
        **{f"reason_{k}": n for k, n in sorted(by_reason.items())},
    }


__all__ = [
    "DEFAULT_LEASE_HOURS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_REQUEUE_AFTER_MINUTES",
    "DEFAULT_SURFACE_AFTER_HOURS",
    "RETRYABLE_FAILURES",
    "Action",
    "ReapPolicy",
    "Reason",
    "TaskSnapshot",
    "Verdict",
    "assess_task",
    "summarise",
]
