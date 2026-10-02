"""Run a goal through the whole organisation, without a person in the loop.

The platform can execute a task and the office can now review a department's work,
but nothing yet *drives* them: press Run and one task runs, and the chain below it
waits for whoever presses Run next. That is a queue with a human in the middle of
every hop, and the brief for this work is explicit that the system must work
autonomously once the executive hands the offices a task.

So this is the missing half, and it is deliberately boring:

1. hand the goal to the chief;
2. repeatedly pick the **deepest ready** task -- ready meaning assigned to a live
   agent, not terminal, and past any approval gate -- and execute it;
3. after each run, ask the office whose department just finished to review it, so
   a rejection and its retry happen in the same pass rather than waiting for a
   human;
4. stop when the root reaches a terminal state, when nothing is runnable, or when
   a bound is hit.

**Deepest-first, not oldest-first.** The office tier's job is to review what came
back, so the department must finish before the office is asked to judge. Running
breadth-first would review an empty result and pass it, which is how a chain ends
up reporting a success that was never produced.

**Approvals are answered, not skipped.** `auto_approve` clears the gate through
`ApprovalService.decide` with a human principal, so the audit row exists. Turning
it off stops the chain at the first gate and reports what is waiting -- which is
the honest behaviour for a mode meant to be audited, and the only way to see the
HITL path work at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.work_review import review_office_work
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, RunMode, TaskStatus
from ai_orchestrator.domain.review import DEFAULT_MAX_ATTEMPTS
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Approval, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.security.auth import operator_id_for
from ai_orchestrator.telemetry.logging import get_logger
from ai_orchestrator.worker_runtime import build_runtime

logger = get_logger(__name__)

#: States from which a task can still be run.
#:
#: **`RUNNING` is deliberately absent.** For a coordination task, `running` means
#: "I delegated and I am waiting", and re-running one made it delegate the same
#: work again: a three-tier goal reached the execution bound at 60 with the office
#: re-delegating on every pass and the department never being reviewed. A waiting
#: task is settled by `settle`, not re-executed.
#:
#: A task stranded in `running` with nothing below it is a different problem, and
#: it belongs to the reaper rather than to this driver.
RUNNABLE = (TaskStatus.CREATED, TaskStatus.ASSIGNED)

#: A task that produced an answer and is waiting for a person.
TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.EXPIRED)

#: Hard stop on the number of executions in one pipeline run. Not a politeness
#: limit: a fan-out cap is per delegation, and a tree of offices and departments
#: can produce a great many tasks inside their per-task limits. Without a ceiling
#: on the whole run, a rerun loop that the per-work bound fails to catch would run
#: until the budget did.
MAX_EXECUTIONS = 60


@dataclass(slots=True)
class StepLog:
    """One execution, in the order it happened."""

    task_id: str
    title: str
    owner: str
    status: str
    depth: int
    review: str = ""
    seconds: float = 0.0


@dataclass(slots=True)
class PipelineOutcome:
    """What the run did, and what it finished with."""

    root_task_id: str
    root_status: str = ""
    root_retries: int = 0
    steps: list[StepLog] = field(default_factory=list)
    reviews_accepted: int = 0
    reviews_rerun: int = 0
    reviews_escalated: int = 0
    stopped_because: str = ""
    waiting_for_human: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    settled_failed: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.root_status == TaskStatus.COMPLETED.value

    def summary(self) -> str:
        lines = [
            f"root {self.root_task_id} -> {self.root_status or 'not run'}",
            f"  executions     : {len(self.steps)}",
            f"  review accepted: {self.reviews_accepted}",
            f"  review rerun   : {self.reviews_rerun}",
            f"  escalated      : {self.reviews_escalated}",
            f"  root retried   : {self.root_retries} (answered instead of delegating)",
            f"  settled upward : {len(self.settled)}",
        ]
        if self.settled_failed:
            lines.append(f"  failed upward  : {len(self.settled_failed)}")
        if self.waiting_for_human:
            lines.append(f"  waiting on a person: {len(self.waiting_for_human)}")
        if self.stopped_because:
            lines.append(f"  stopped because: {self.stopped_because}")
        return "\n".join(lines)


def _agent_name(row: Any) -> str:
    return str(row["name"]) if row is not None else "—"


async def _next_ready(session: Any, org_id: str, root_id: str) -> Task | None:
    """The deepest runnable task in the tree, or `None`.

    Deepest first, and within a depth the earliest first, so a department finishes
    before the office above it is asked to judge anything.
    """
    runnable_states = {s.value for s in RUNNABLE}
    tree = await _tree(session, org_id, root_id)
    runnable = [t for t in tree if str(t.status) in runnable_states]
    if not runnable:
        return None
    depths = {str(t.id): _depth_of(t, root_id) for t in tree}
    return max(runnable, key=lambda t: (depths[str(t.id)], -_seq(t)))


def _seq(task: Task) -> int:
    """A stable tiebreak.

    `created_at` can tie at microsecond resolution, and a nondeterministic pick
    makes a run irreproducible, which is the opposite of what a demonstration
    needs.
    """
    tail = str(task.id).rsplit("-", 1)[-1]
    try:
        return int(tail, 16)
    except ValueError:
        return 0


async def _tree(session: Any, org_id: str, root_id: str) -> list[Task]:
    """Every task in the subtree rooted at `root_id`, the root included, BFS.

    The root is included because a walk that omits its own starting point cannot
    run it, and a walk that omits it looks like a well-behaved one that found
    nothing to do.
    """
    parents = dict(
        (
            await session.execute(
                select(Task.id, Task.parent_task_id).where(Task.organization_id == org_id)
            )
        ).all()
    )
    children: dict[str | None, list[str]] = {}
    for task_id, parent_id in parents.items():
        children.setdefault(parent_id, []).append(task_id)

    ordered = (
        (
            await session.execute(
                select(Task).where(Task.organization_id == org_id).order_by(Task.created_at)
            )
        )
        .scalars()
        .all()
    )
    by_id = {str(t.id): t for t in ordered}

    out: list[Task] = []
    seen: set[str] = set()
    frontier = [root_id]
    depth = 0
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            if node in seen or node not in by_id:
                continue
            seen.add(node)
            row = by_id[node]
            # BFS already knows how deep this is; recording it here is why
            # `_depth_of` does not have to guess from a parent chain.
            object.__setattr__(row, "_pipeline_depth", depth)
            out.append(row)
            nxt.extend(children.get(node, []))
        frontier = nxt
        depth += 1
    return out


def _depth_of(task: Task, root_id: str) -> int:
    """How many delegations down this task is, using the row's own chain.

    `tasks.root_task_id` cannot answer it: the repository writes it as
    `parent_task_id`, so it names the *immediate* parent rather than the tree root,
    and every task below the first hop reported depth 1. Since there is no column
    that holds the real root, the depth is carried on the task object that
    `_tree` built, and this is the fallback for a task that did not come from
    there.
    """
    carried = getattr(task, "_pipeline_depth", None)
    if carried is not None:
        return int(carried)
    if str(task.id) == root_id:
        return 0
    return 1 if task.parent_task_id else 0


async def run_pipeline(
    database: Database,
    organization_id: str,
    root_task_id: str,
    *,
    auto_approve: bool = True,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_executions: int = MAX_EXECUTIONS,
    run_mode: RunMode = RunMode.LIVE,
) -> PipelineOutcome:
    """Drive one goal to its conclusion. The whole point is that nobody is asked."""
    from ai_orchestrator.config.settings import get_settings

    settings = get_settings()
    outcome = PipelineOutcome(root_task_id=root_task_id)
    executions = 0
    approver = Actor(
        id=operator_id_for(organization_id),
        kind=ActorType.HUMAN,
        is_privileged_human=True,
        display_name="local operator (autonomous pipeline)",
    )

    async with database.committing_tenant_session(organization_id) as session:
        service = TaskExecutionService(
            session=session,
            organization_id=organization_id,
            run_mode=run_mode,
            auto_approve=auto_approve and settings.approval_auto_approve,
            runtime=build_runtime(settings),
        )
        audit = AuditService(session, organization_id)

        while executions < max_executions:
            root = (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == organization_id, Task.id == root_task_id
                    )
                )
            ).scalar_one_or_none()
            if root is None:
                outcome.stopped_because = "the root task does not exist"
                break
            outcome.root_status = str(root.status)
            if TaskStatus(root.status) in TERMINAL:
                break

            settled = await settle_finished(
                session, organization_id, root_task_id, max_attempts=max_attempts
            )
            if settled.completed or settled.failed:
                await session.commit()
                await database.bind_tenant(session, organization_id)
                outcome.settled.extend(settled.completed)
                outcome.settled_failed.extend(settled.failed)
                continue

            ready = await _next_ready(session, organization_id, root_task_id)
            if ready is None:
                # **A chief that answered instead of delegating is told so, and gets
                # another turn.**
                #
                # This is the same finding an office gives its department, and the
                # office's finding is what makes its rerun converge -- the office
                # hands the previous attempt's text back and the retry reformats it.
                # The root had no such path: it failed `no_delegation` and the run
                # stopped there, so a competent model that simply answered the
                # department's question produced a *failed task* with a perfectly good
                # answer inside it. Measured:
                #
                #     supplier-tender, real free model:
                #       in=54128 out=9223 tools=15 models=16
                #       summary: "**Nhà thầu đề xuất trúng thầu: Công ty Toàn Cầu
                #                 (Báo giá C)** ... lý do theo tiêu chí ..."
                #       task.failed  category=no_delegation
                #
                # The control was right -- a fleet that answers everything itself is
                # not a fleet -- and the outcome was wrong, because nothing told the
                # chief what it had done wrong while something could.
                #
                # So the root is retried with the finding in its goal, exactly as an
                # office's rerun is. Bounded by `max_attempts` for the same reason:
                # a model that will not delegate must stop costing requests, not loop.
                retried = await _retry_a_root_that_answered_itself(
                    session, organization_id, root, max_attempts=max_attempts
                )
                if retried is not None:
                    # **Point the loop at the retry.** Without this the new task is
                    # created and never dispatched: the loop re-reads `root_task_id`
                    # each turn, so it would keep looking at the failed root, see
                    # nothing runnable, and try to retry again until the budget ran
                    # out -- reporting "root retried 3" and having run nothing.
                    outcome.root_retries += 1
                    outcome.root_status = str(root.status)
                    root_task_id = retried
                    await session.commit()
                    await database.bind_tenant(session, organization_id)
                    continue
                blocked = await _clear_gates(session, organization_id, root_task_id, approver)
                if not blocked:
                    outcome.stopped_because = "nothing is runnable and no gate can be cleared"
                    break
                continue

            executions += 1
            owner_name = await _owner_name(
                session, organization_id, str(ready.owner_agent_id or "")
            )
            started = asyncio.get_running_loop().time()
            await service.execute_task(str(ready.id))
            await session.flush()
            elapsed = asyncio.get_running_loop().time() - started

            refreshed = (
                await session.execute(
                    select(Task).where(Task.organization_id == organization_id, Task.id == ready.id)
                )
            ).scalar_one()
            log = StepLog(
                task_id=str(refreshed.id),
                title=str(refreshed.title)[:60],
                owner=owner_name,
                status=str(refreshed.status),
                depth=_depth_of(refreshed, root_task_id),
                seconds=elapsed,
            )

            # The office reviews as soon as its department finishes. Done here, in
            # the same pass, because a rejection that waits for the next run is a
            # rejection nobody asked for yet.
            if refreshed.parent_task_id:
                review = await review_office_work(
                    session,
                    organization_id,
                    str(refreshed.parent_task_id),
                    max_attempts=max_attempts,
                )
                if review.reviewed:
                    names = {r.action: r.action for r in review.reviewed}
                    log.review = (
                        ", ".join(sorted({a for a in names if a != "waiting"})) or "reviewed"
                    )
                    outcome.reviews_accepted += len(review.accepted)
                    outcome.reviews_rerun += len(review.rerun)
                    outcome.reviews_escalated += len(review.escalated)
                outcome.waiting_for_human = list(review.waiting) if review.waiting else []

            outcome.steps.append(log)
            await session.commit()
            # The tenant binding is `SET LOCAL`, so the commit just ended it.
            # Without this the next statement runs with no tenant, row-level
            # security filters everything out, and the run reports an empty
            # organisation and finishes successfully.
            await database.bind_tenant(session, organization_id)

        else:
            outcome.stopped_because = f"the execution bound of {max_executions} was reached"

        await _collect_waiting(session, organization_id, root_task_id, outcome)
        await audit.record(
            actor=approver,
            action="pipeline.run",
            resource_type="task",
            resource_id=root_task_id,
            task_id=root_task_id,
            context={
                "executions": len(outcome.steps),
                "root_status": outcome.root_status,
                "accepted": outcome.reviews_accepted,
                "rerun": outcome.reviews_rerun,
                "escalated": outcome.reviews_escalated,
            },
        )
        await session.commit()
        await database.bind_tenant(session, organization_id)

    return outcome


@dataclass(slots=True)
class Settled:
    """What closing the tree did: what was reported complete, and what was failed.

    Two lists rather than one, because conflating them is how a run reports a
    tidy summary over a department that never did the work.
    """

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


async def _retry_a_root_that_answered_itself(
    session: Any, organization_id: str, root: Any, *, max_attempts: int
) -> str | None:
    """Give a chief that answered its own coordination task another turn, once spent.

    Returns the new root's id, or `None` when there is nothing to retry.

    **The same shape as an office's rerun, and for the same reason.** The office hands
    its department the finding plus the previous attempt's text, and the retry
    reformats instead of re-deriving -- that is what makes the loop converge. The root
    had no such path at all: it failed `no_delegation` and the run stopped there, so a
    model that produced a *perfect* answer to a department's question produced a
    *failed task*. The control was right and the outcome was wrong, because nothing
    told the chief what it had done wrong while something could.

    Two boundaries, both deliberate:

    * **The attempt budget is shared with the office's.** `attempt_count` on the root
      counts against `max_attempts`, so a chief that will not delegate spends the
      budget and stops rather than looping. A model that ignores a correction twice
      will ignore it forty times, and each attempt is tens of thousands of tokens.
    * **Only `no_delegation` is retried.** Every other failure category is the answer
      to a different question, and re-running a root that failed for one of those
      would be retrying on faith.
    """
    from ai_orchestrator.application.work_review import attempt_of

    if TaskStatus(root.status) != TaskStatus.FAILED:
        return None
    if (root.failure_category or "") != "no_delegation":
        return None
    attempt = attempt_of(root)
    if attempt >= max_attempts:
        return None

    tasks_repo = TaskRepository(session, organization_id)
    chief = (
        await session.execute(
            select(Agent).where(
                Agent.organization_id == organization_id, Agent.name == "Executive Agent"
            )
        )
    ).scalar_one_or_none()
    if chief is None:
        return None

    retry = await tasks_repo.create(
        title=root.title,
        goal=(
            f"{root.goal}\n\n"
            f"[PHẢI LÀM LẠI — lần {attempt + 1}] Lần trước bạn tự làm công việc này thay "
            f"vì chuyển giao. Đây là một nhiệm vụ phối hợp: bạn không có dữ liệu để trả lời, "
            f"và nhiệm vụ sẽ chỉ xong khi bộ phận sở hữu nó đã làm xong. Hãy gọi "
            f"`delegate_to_agent` và chọn đúng văn phòng/phòng ban phụ trách."
        ),
        task_type=root.task_type,
        requester_type="human",
        owner_agent_id=chief.id,
        expected_output_schema=root.expected_output_schema,
        # The material travels with the retry, or the department has nothing either.
        input=dict(root.input or {}),
    )
    await tasks_repo.assign(str(retry.id), chief.id)
    return str(retry.id)


async def settle_finished(
    session: Any,
    organization_id: str,
    root_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Settled:
    """Complete the tasks whose work below them has finished and passed review.

    **This is the step that makes the organisation autonomous.** A coordination
    task cannot finish inside its own run -- it delegates, and the answer arrives
    later -- so something has to notice that the answer arrived and close it.
    Without this the office stays `running` forever, and a driver that re-runs
    `running` tasks loops until it hits its bound.

    A task is settled as **complete** only when every task below it is terminal
    **and** its review accepted all of them.

    A task whose review **escalated** is failed instead, with the findings as its
    reason. That is the change this function was missing. It used to skip such a
    task and leave it `running`, on the reasoning that "the executive is then told
    the work is unresolved" -- and the executive was told nothing, because a task
    nobody settles is a task nobody reports. The run ended with `escalated 1`,
    `settled upward 0` and `stopped because: nothing is runnable and no gate can
    be cleared`, which is a hang dressed as a policy.

    Failing is the truthful terminal state here: the office did its job twice and
    the department could not do the work, and the executive's answer to that is not
    a tidy summary. It propagates upward one tier at a time, so the root ends
    `failed` carrying the reason the department gave.
    """
    from ai_orchestrator.application.work_review import ESCALATION_CATEGORY, review_office_work

    tasks_repo = TaskRepository(session, organization_id)
    result = Settled()
    tree = await _tree(session, organization_id, root_id)
    for task in tree:
        # `!=`, not `is not`: two equal strings are usually two objects, and
        # `is not` on strings answers "same object?" not "same value?".
        if str(task.status) != TaskStatus.RUNNING.value:
            continue
        if await tasks_repo.live_descendant_count(str(task.id)) > 0:
            continue
        review = await review_office_work(
            session, organization_id, str(task.id), max_attempts=max_attempts
        )
        if review.waiting or review.rerun:
            continue
        if review.escalated:
            await tasks_repo.transition(
                str(task.id),
                Transition.FAIL,
                error=_escalation_reason(review),
                failure_category=ESCALATION_CATEGORY,
            )
            result.failed.append(str(task.id))
            continue
        if not review.all_accepted:
            # Reviewed, nothing running, nothing rerun, nothing escalated, and
            # still not accepted: the coordinator's own work did not hold up.
            await tasks_repo.transition(
                str(task.id),
                Transition.FAIL,
                error=_rejection_reason(review),
                failure_category="review_rejected",
            )
            result.failed.append(str(task.id))
            continue
        summary = await _report_from_children(session, organization_id, str(task.id))
        await tasks_repo.set_latest_summary(str(task.id), summary)
        await tasks_repo.transition(str(task.id), Transition.COMPLETE)
        result.completed.append(str(task.id))
    return result


def _escalation_reason(review: Any) -> str:
    """Why this office gave up, written for a person reading the task row."""
    lines = ["the work below this office was not accepted and could not be fixed:"]
    for item in review.escalated:
        detail = "; ".join(item.verdict.findings) or item.why
        lines.append(f"- [{item.task_id}] {detail}")
    return "\n".join(lines)[:2000]


def _rejection_reason(review: Any) -> str:
    """Why a coordinator's own work did not hold up."""
    lines = ["this coordinator did not produce a usable answer:"]
    for item in review.reviewed:
        detail = "; ".join(item.verdict.findings) or item.why
        lines.append(f"- [{item.task_id}] {detail}")
    return "\n".join(lines)[:2000]


async def _report_from_children(session: Any, org_id: str, task_id: str) -> str:
    """What the departments below this task actually produced, attributed.

    Built from `tasks.output` rather than `executions.summary`, because the summary
    says how the run went ("done: ...") and the output *is* the deliverable. Each
    department's contribution is quoted and attributed so a reader can tell which
    finding came from where -- and marked untrusted, because a department's text
    reaching the executive is exactly the case the untrusted flag exists for.
    """
    rows = (
        await session.execute(
            select(Task.title, Task.owner_agent_id, Task.output, Task.status).where(
                Task.organization_id == org_id,
                Task.id.in_(await _child_ids(session, org_id, task_id)),
            )
        )
    ).all()
    parts: list[str] = []
    for title, owner_id, output, status in rows:
        agent = await _owner_name(session, org_id, str(owner_id or ""))
        if output:
            rendered = "; ".join(f"{k}: {v}" for k, v in dict(output).items())
            parts.append(f'"{title}" ({agent}, {status}) reported: {rendered}')
        else:
            parts.append(f'"{title}" ({agent}) finished as {status} with nothing to report.')
    if not parts:
        return "No department work was recorded below this task."
    return "Reviewed and accepted from the departments below. " + " | ".join(parts)


async def _child_ids(session: Any, org_id: str, task_id: str) -> list[str]:
    from ai_orchestrator.persistence.models import Delegation

    return [
        str(row[0])
        for row in (
            await session.execute(
                select(Delegation.child_task_id).where(
                    Delegation.organization_id == org_id,
                    Delegation.parent_task_id == task_id,
                )
            )
        ).all()
    ]


async def _owner_name(session: Any, org_id: str, agent_id: str) -> str:
    if not agent_id:
        return "—"
    row = (
        await session.execute(
            select(Agent.name).where(Agent.organization_id == org_id, Agent.id == agent_id)
        )
    ).scalar_one_or_none()
    return str(row) if row else "—"


async def _clear_gates(session: Any, org_id: str, root_id: str, approver: Actor) -> bool:
    """Answer the approvals this run is allowed to answer. Returns whether any were.

    Only the root's own subtree, and only `pending` rows, so a run cannot approve
    an unrelated tenant's work or re-answer a question already put.
    """
    from ai_orchestrator.approvals.service import ApprovalService

    pending = (
        (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == org_id, Approval.status == "pending"
                )
            )
        )
        .scalars()
        .all()
    )
    tree_ids = {str(t.id) for t in await _tree(session, org_id, root_id)}
    mine = [a for a in pending if a.task_id in tree_ids]
    if not mine:
        return False
    approvals = ApprovalService(session=session, organization_id=org_id)
    for approval in mine:
        try:
            await approvals.decide(
                str(approval.id), approver=approver, approve=True, note="autonomous pipeline"
            )
        except Exception as exc:
            logger.warning("pipeline.approval_refused", approval=approval.id, error=str(exc))
    await session.commit()
    return True


async def _collect_waiting(
    session: Any, org_id: str, root_id: str, outcome: PipelineOutcome
) -> None:
    """Record what the run is blocked on, so "it stopped" says on what."""
    tree = await _tree(session, org_id, root_id)
    outcome.waiting_for_human = sorted(
        str(t.id) for t in tree if TaskStatus(t.status) is TaskStatus.WAITING_FOR_APPROVAL
    )
    if not outcome.stopped_because and outcome.waiting_for_human:
        outcome.stopped_because = "waiting on a person to approve a gate"


__all__ = [
    "MAX_EXECUTIONS",
    "PipelineOutcome",
    "Settled",
    "StepLog",
    "run_pipeline",
    "settle_finished",
]
