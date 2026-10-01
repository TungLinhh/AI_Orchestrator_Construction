"""The office as a manager: review what a department produced, and send back what fails.

This is the loop the organisation is missing. Today a chain runs top to bottom and
whatever the department writes is what reaches the executive -- a run that declared
`approved_headcount` and produced `{"scripted": true, "proposal_count": 0}` was
caught only by a gate that refused to mark it complete, and a run whose output was
`[draft] <key>` for every key was marked complete. The middle tier is supposed to
be the thing that notices, and it has nothing to notice with.

**Why this is a service and not a step inside the office's own run.** The office's
run ends when it delegates; the department's run happens later, in a different
`execute_task`, driven by a different caller. An agent cannot review a result that
does not exist yet, so the review cannot live in the office's prompt. It is a step
between two runs, which is exactly where a supervisor sits in a real organisation:
not inside the manager's shift, but between the manager handing work out and the
answer coming back.

**What it does, per department task under the office:**

* wait for it to reach a terminal state (a task still running is not judged);
* apply the deterministic checks in `domain.review`;
* on a pass, record the acceptance;
* on a failure, **send it back** -- a new delegated task, to the same department,
  carrying the findings as its brief, with the attempt number incremented;
* when the attempt bound is reached, stop sending back and record an escalation,
  because a manager who rejects forever is not managing either.

Everything it decides is written to the audit log, so "why was this sent back
twice" has an answer that is not a memory of a model run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.delegation import DelegationLimits
from ai_orchestrator.domain.enums import ActorType, TaskStatus
from ai_orchestrator.domain.review import (
    DEFAULT_MAX_ATTEMPTS,
    ReviewVerdict,
    assess_output,
    should_rerun,
)
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.repositories.task import DelegationRepository, TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Audit action for an acceptance. Separate from the rejection so a query can
#: answer "how much of this office's work passed review" without parsing prose.
ACTION_ACCEPTED = "task.review_accepted"
ACTION_REJECTED = "task.review_rejected"
ACTION_RERUN = "task.review_rerun_ordered"
ACTION_ESCALATED = "task.review_escalated"

#: Terminal states a department task can be judged in. `failed` is included on
#: purpose: a department that crashed has not produced a good answer, and treating
#: a crash as "nothing to review" would let a broken department look idle.
JUDGEABLE = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.EXPIRED)

#: Written into `tasks.failure_category` when a task is failed *because* the review
#: escalated it, as opposed to because its run crashed.
#:
#: This is the distinction between the two reasons a task below the office can be
#: `failed`, and it decides whether it may be sent back. A crash is transient and is
#: retried. An escalation has already spent the retry budget and is not. Both are
#: `failed`, so the status alone cannot tell them apart -- and reading it as though it
#: could was the first version of this rule, which broke the retry-on-crash behaviour
#: that `test_a_failed_department_run_is_sent_back_not_ignored` was written to keep.
#:
#: `pipeline.settle_finished` writes the same value, which is why it lives here: two
#: modules that must agree on a string should not each hold their own copy of it.
ESCALATION_CATEGORY = "review_escalated"


@dataclass(slots=True)
class ReviewedTask:
    """One department task, and what the office decided about it."""

    task_id: str
    agent_id: str
    attempt: int
    verdict: ReviewVerdict
    action: str  # accepted | rerun | escalated | waiting
    rerun_task_id: str | None = None
    why: str = ""


@dataclass(slots=True)
class ReviewOutcome:
    """The office's review of everything under it."""

    office_task_id: str
    office_agent_id: str
    reviewed: list[ReviewedTask] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> list[ReviewedTask]:
        return [r for r in self.reviewed if r.action == "accepted"]

    @property
    def rerun(self) -> list[ReviewedTask]:
        return [r for r in self.reviewed if r.action == "rerun"]

    @property
    def escalated(self) -> list[ReviewedTask]:
        return [r for r in self.reviewed if r.action == "escalated"]

    @property
    def all_accepted(self) -> bool:
        """Whether the office may now report upward.

        `waiting` counts as not-accepted on purpose: a department task still
        running means the office does not yet have an answer to give the
        executive, and reporting "done" over a running task is how a chain reports
        success having produced nothing.
        """
        return bool(self.reviewed) and not self.waiting and not self.rerun and not self.escalated


def work_key(task: Task) -> str:
    """A stable identity for the work, so a rerun is recognisable as the same work.

    Read from the task's own `input`, and falling back to the task id so a task
    created before this existed still has a key. It is deliberately not derived
    from the goal: the rerun's goal *differs* from the original, because it carries
    the findings. Keying on the goal would make every retry look like new work.
    """
    return str((task.input or {}).get("work_key") or task.id)


def attempt_of(task: Task) -> int:
    """Which try this is. First try is 1, and absent means first try."""
    try:
        return max(1, int((task.input or {}).get("attempt", 1)))
    except TypeError, ValueError:
        return 1


async def self_review_of_a_coordinator(
    session: Any, org_id: str, task: Task, *, attempt: int
) -> ReviewVerdict:
    """Was this coordinator's own work finished?

    Three things, all facts rather than opinions: it delegated something,
    everything it delegated has reached a terminal state, and **everything it
    delegated actually succeeded**.

    The third check is the one that was missing, and it is why an escalation used
    to end a run in a hang instead of in an answer. "All terminal" is satisfied by
    a child that *failed*, so an office whose department had been sent back twice
    and escalated passed its own review and reported upward as if it had an answer.
    Measured on a real run: `escalated 1`, `settled upward 0`, root left at
    `running` with `stopped because: nothing is runnable and no gate can be cleared`
    -- the executive was told nothing at all, which is not "told the work is
    unresolved".

    A coordinator whose child failed has not done its job, so it fails too, and the
    reason carries upward until the root states the truth.
    """
    from ai_orchestrator.domain.review import Check

    children = await _children(session, org_id, str(task.id))
    terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.EXPIRED)
    live = [c for c in children if TaskStatus(c.status) not in terminal]
    broken = [
        c for c in children if TaskStatus(c.status) in (TaskStatus.FAILED, TaskStatus.EXPIRED)
    ]
    checks = (
        Check("delegated", bool(children), f"{len(children)} task(s) below"),
        Check("work_finished", not live, f"{len(live)} still open"),
        Check(
            "children_succeeded",
            not broken,
            "all below completed" if not broken else f"{len(broken)} of {len(children)} failed",
        ),
    )
    ok = all(c.passed for c in checks)
    findings: tuple[str, ...] = ()
    if live:
        findings = ("it delegated work that has not finished, so it has nothing to report yet",)
    elif broken:
        why = "; ".join(f"{c.title[:60]} -> {c.status}" for c in broken[:5])
        findings = (
            f"the work below it did not succeed, so it cannot report an answer. "
            f"{len(broken)} of {len(children)} task(s) failed or expired: {why}",
        )
    elif not children:
        findings = (
            "it has no delegated work below it, so it is not a coordinator "
            "and must answer for itself",
        )
    return ReviewVerdict(ok=ok, checks=checks, findings=findings)


def _brief_of(child: Task) -> str:
    """Everything the department was told, as one block of text.

    The echo check compares the answer against the ask, so it needs the whole ask:
    the goal, the title, and the `brief` the delegation carried. A retry carries all
    three *plus* the review findings, and the findings are the office's words rather
    than the brief's -- including them would let a department hide a restatement
    behind the very text it was sent back for not restating.
    """
    payload = child.input or {}
    parts = [str(child.goal or ""), str(child.title or "")]
    for key in ("brief", "objective"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _failed_child_verdict(child: Task) -> ReviewVerdict:
    """The verdict on a child that is itself `failed` or `expired`.

    It carries the child's own reason rather than inventing one, because the child
    wrote down exactly what went wrong -- often the department's findings, several
    tiers down -- and that text is what the executive needs.
    """
    from ai_orchestrator.domain.review import Check

    reason = (child.last_error or "").strip() or f"it ended {child.status} with no reason recorded"
    return ReviewVerdict(
        ok=False,
        checks=(Check("child_succeeded", False, str(child.status)),),
        findings=(f"the work it was given did not succeed: {reason}",),
    )


def _current_attempts(children: list[Task]) -> list[Task]:
    """Only the newest attempt of each piece of work, in first-seen order.

    **A bug this fixes, found by a test that asserted a bound of two.** The bound
    was being read off each task row's own `attempt`, so a rejected first attempt
    that had *already been superseded* by a second still counted as having budget
    left and was sent back again -- three tasks for two attempts, and the bound
    that exists precisely to stop that loop was not stopping it.

    The attempt number belongs to the **work**, not to a row. So the office judges
    the latest attempt of each `work_key` and ignores the history: a superseded
    attempt has already been answered for, and re-judging it produces a rerun of a
    finding nobody is still waiting on.
    """
    newest: dict[str, Task] = {}
    order: list[str] = []
    for child in children:
        key = work_key(child)
        current = newest.get(key)
        if current is None:
            newest[key] = child
            order.append(key)
            continue
        if (attempt_of(child), str(child.id)) > (attempt_of(current), str(current.id)):
            newest[key] = child
    return [newest[key] for key in order]


async def _children(session: Any, org_id: str, office_task_id: str) -> list[Task]:
    """Every task the office delegated, oldest first.

    From `delegations`, not from a `parent_task_id` walk: the delegation row is
    the record of the *act of delegating*, including the reruns this module
    creates, and it is the thing a reviewer would read.
    """
    from ai_orchestrator.persistence.models import Delegation

    rows = (
        (
            await session.execute(
                select(Task)
                .join(Delegation, Delegation.child_task_id == Task.id)
                .where(
                    Delegation.organization_id == org_id,
                    Delegation.parent_task_id == office_task_id,
                )
                .order_by(Task.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def review_office_work(
    session: Any,
    organization_id: str,
    office_task_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ReviewOutcome:
    """The office reviews every department task under `office_task_id`.

    Safe to call repeatedly, which is the point: the pipeline driver calls it after
    each run, and a department task still running is reported as `waiting` rather
    than judged, so calling it early is not an error and calling it twice on a
    finished tree changes nothing.
    """
    office = (
        await session.execute(
            select(Task).where(Task.organization_id == organization_id, Task.id == office_task_id)
        )
    ).scalar_one_or_none()
    if office is None:
        return ReviewOutcome(office_task_id=office_task_id, office_agent_id="")

    office_agent_id = str(office.owner_agent_id or "")
    outcome = ReviewOutcome(office_task_id=office_task_id, office_agent_id=office_agent_id)
    audit = AuditService(session, organization_id)
    tasks_repo = TaskRepository(session, organization_id)

    children = await _children(session, organization_id, office_task_id)
    for child in _current_attempts(children):
        if TaskStatus(child.status) not in JUDGEABLE:
            outcome.waiting.append(str(child.id))
            continue

        agent_id = str(child.owner_agent_id or "")
        attempt = attempt_of(child)

        # **A child that failed has produced nothing, so it cannot be accepted.**
        #
        # Checked from the child's *own* status, before any judgement about its output.
        # The reason is the shape of the review below it: a coordinator is judged by a
        # self-review, so the parent asks "did you coordinate", the coordinator answers
        # from its own children, and neither of them ever looks at whether the
        # coordinator itself finished.
        #
        # Measured: an office whose department was sent back twice and escalated sat at
        # `failed` carrying the reason, and the root still settled `completed` —
        # `failed upward 1`, `settled upward 1`, `root -> completed`. A run reported
        # success over a department that never did the work.
        if TaskStatus(child.status) in (TaskStatus.FAILED, TaskStatus.EXPIRED):
            verdict = _failed_child_verdict(child)
        else:
            # **A child that delegated is a coordinator, and is judged as one.**
            #
            # The contract on a coordinator is the *worker's* contract, inherited on the
            # way down, and a coordinator that produced no `verdicts` of its own was
            # being rejected by the very office above it that it had just reported to.
            # Measured: the root settled as `running` forever, "nothing is runnable and
            # no gate can be cleared", because the office's review of the chief failed
            # it for not reproducing the department's keys.
            #
            # So the question for a coordinator is not "did you produce the worker's
            # output" but "did you coordinate": did it delegate, is everything it
            # delegated finished, and did all of it succeed. The answer itself was
            # already checked, on the department.
            delegated_down = bool(await _children(session, organization_id, str(child.id)))
            if delegated_down:
                verdict = await self_review_of_a_coordinator(
                    session, organization_id, child, attempt=attempt
                )
            else:
                verdict = assess_output(
                    output=child.output,
                    expected_output_schema=child.expected_output_schema,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    source_text=_brief_of(child),
                )

        if verdict.ok:
            await audit.record(
                actor=Actor(id=office_agent_id, kind=ActorType.AGENT),
                action=ACTION_ACCEPTED,
                resource_type="task",
                resource_id=str(child.id),
                task_id=office_task_id,
                context={
                    "agent": agent_id,
                    "attempt": attempt,
                    "checks": len(verdict.checks),
                },
            )
            outcome.reviewed.append(
                ReviewedTask(
                    task_id=str(child.id),
                    agent_id=agent_id,
                    attempt=attempt,
                    verdict=verdict,
                    action="accepted",
                    why="every check passed",
                )
            )
            continue

        child_failed = TaskStatus(child.status) in (TaskStatus.FAILED, TaskStatus.EXPIRED)
        already_escalated = child_failed and child.failure_category == ESCALATION_CATEGORY
        if already_escalated:
            # **Never re-dispatch a task that already escalated.**
            #
            # Not because it failed -- a crash *is* sent back, because a crash is often
            # transient and treating it as unjudgeable would let a broken department
            # look idle. But *this* failure is the review's own verdict on a work item
            # that has already spent its whole retry budget, and asking again spends a
            # second budget on a third attempt at the same question.
            #
            # Written first as "never retry a failed task", which broke two existing
            # tests and would have broken the retry-on-crash behaviour on purpose. The
            # distinction that matters is *who* decided, not *what* the status is.
            again, why = (
                False,
                (
                    "the task below was already escalated after its full retry budget, "
                    "so re-dispatching it would ask the same question a third time -- "
                    "escalating with its reason instead"
                ),
            )
        else:
            again, why = should_rerun(verdict, attempt=attempt, max_attempts=max_attempts)
        await audit.record(
            actor=Actor(id=office_agent_id, kind=ActorType.AGENT),
            action=ACTION_REJECTED,
            resource_type="task",
            resource_id=str(child.id),
            task_id=office_task_id,
            context={"agent": agent_id, "attempt": attempt, "findings": list(verdict.findings)},
        )

        if not again:
            outcome.reviewed.append(
                ReviewedTask(
                    task_id=str(child.id),
                    agent_id=agent_id,
                    attempt=attempt,
                    verdict=verdict,
                    action="escalated",
                    why=why,
                )
            )
            await audit.record(
                actor=Actor(id=office_agent_id, kind=ActorType.AGENT),
                action=ACTION_ESCALATED,
                resource_type="task",
                resource_id=str(child.id),
                task_id=office_task_id,
                context={"agent": agent_id, "attempts": attempt, "reason": why},
            )
            logger.warning(
                "review.escalated", task_id=child.id, office=office_task_id, agent=agent_id
            )
            continue

        # Send it back. `allow_parallel=True` because the original attempt is a
        # finished task that still holds this fingerprint, and the dedup is right
        # to refuse: this is the *same* work asked of the *same* agent, which is
        # the case the guard exists for. The retry is a deliberate exception with a
        # review attached, not an accident.
        rerun = await tasks_repo.create(
            title=f"{child.title[:90]} (lần {attempt + 1})",
            goal=_rerun_goal(child, verdict),
            task_type=child.task_type,
            parent_task_id=office_task_id,
            owner_agent_id=agent_id,
            requester_type="agent",
            requester_agent_id=office_agent_id or None,
            expected_output_schema=child.expected_output_schema,
            input={
                # **The material comes with it.** The first version of the retry
                # carried only `work_key`, `attempt` and the findings -- so the
                # department was asked to do the work again with the work
                # stripped out, and every retry failed for the same reason as the
                # attempt before it. Measured on a real free-model run: the
                # original had `brief` and a 1043-character goal, the retry had
                # neither and failed `output_contract_unmet`, and the office
                # escalated a piece of work that had never been given to anyone a
                # second time.
                #
                # So the retry inherits the routing facts and the brief, and adds
                # the findings. A department sent back must be able to see what it
                # was asked and what was wrong with the answer.
                **{
                    key: value
                    for key, value in (child.input or {}).items()
                    if key in ("owning_department", "owning_office", "brief")
                },
                "work_key": work_key(child),
                "attempt": attempt + 1,
                "review": list(verdict.findings),
                "rerun_of": str(child.id),
            },
            allow_parallel=True,
        )
        await DelegationRepository(session, organization_id).record(
            parent_task_id=office_task_id,
            source_agent_id=office_agent_id,
            target_agent_id=agent_id,
            objective=str(rerun.goal)[:200],
            path=_path_for(office, office_agent_id),
            platform_limits=DelegationLimits.platform_default(),
            parent_limits=DelegationLimits.platform_default(),
            child_task_id=str(rerun.id),
        )
        await audit.record(
            actor=Actor(id=office_agent_id, kind=ActorType.AGENT),
            action=ACTION_RERUN,
            resource_type="task",
            resource_id=str(rerun.id),
            task_id=office_task_id,
            context={
                "agent": agent_id,
                "rerun_of": str(child.id),
                "attempt": attempt + 1,
                "findings": list(verdict.findings),
            },
        )
        outcome.reviewed.append(
            ReviewedTask(
                task_id=str(child.id),
                agent_id=agent_id,
                attempt=attempt,
                verdict=verdict,
                action="rerun",
                rerun_task_id=str(rerun.id),
                why=why,
            )
        )
        logger.info(
            "review.rerun_ordered",
            office=office_task_id,
            rejected=child.id,
            rerun=rerun.id,
            agent=agent_id,
        )

    return outcome


def _path_for(office: Task, office_agent_id: str) -> Any:
    """The office's position in the chain, so the rerun's gates apply as they would.

    Built from the office task itself rather than passed in: a rerun ordered by
    this module is a delegation from the office, and it must be subject to the
    same cycle, depth and fan-out checks as the office's own first delegation. A
    retry that skipped them would be a hole in exactly the rules this project spent
    a session closing.
    """
    from ai_orchestrator.domain.delegation import DelegationPath
    from ai_orchestrator.domain.ids import AgentId, TaskId

    if not office_agent_id:
        return DelegationPath.root(AgentId(str(office.id)), TaskId(str(office.id)))
    return DelegationPath.root(AgentId(office_agent_id), TaskId(str(office.id)))


def _rerun_goal(child: Task, verdict: ReviewVerdict) -> str:
    """The brief for the second attempt, written by the office.

    The findings come first and the original ask second, because the department
    already knows what was asked -- it is what it answered badly -- and the only
    thing it lacks is what was wrong with the answer.
    """
    findings = "\n".join(f"- {finding}" for finding in verdict.findings)
    return (
        f"{child.goal}\n\n"
        f"[PHẢI LÀM LẠI — lần {attempt_of(child) + 1}] Bộ phận trưởng đã kiểm tra và "
        f"không chấp nhận kết quả trước vì:\n{findings}\n\n"
        f"Phải sửa đúng các điểm trên. Không được trả lại cùng một câu trả lời."
    )


async def review_office_work_in_session(
    database: Database, organization_id: str, office_task_id: str, **kwargs: Any
) -> ReviewOutcome:
    """The same review in its own transaction, for callers outside a request.

    The pipeline driver has no ambient session, and opening one per review keeps
    the commit boundary honest: a review that ordered a rerun has written a task
    and a delegation row, and both are committed together or not at all.
    """
    async with database.tenant_session(organization_id) as session:
        outcome = await review_office_work(session, organization_id, office_task_id, **kwargs)
        await session.commit()
        return outcome


__all__ = [
    "ACTION_ACCEPTED",
    "ACTION_ESCALATED",
    "ACTION_REJECTED",
    "ACTION_RERUN",
    "ESCALATION_CATEGORY",
    "ReviewOutcome",
    "ReviewedTask",
    "attempt_of",
    "review_office_work",
    "review_office_work_in_session",
    "work_key",
]
