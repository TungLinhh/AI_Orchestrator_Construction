"""Drain a goal through the organisation with bounded, independent workers.

Dispatch the deepest ready work whenever a slot is available. Each worker owns
its transaction; reviews and upward settlement consume committed results. A slow
office does not keep ready departments behind a batch barrier. The driver drains
descendants even if their root fails, and settles work after the last execution.

There is no deadline for the whole goal. Each execution enforces the configured
task deadline and reports a timeout as failure. Intent, retry and execution bounds
prevent unbounded creation. Pending human decisions remain pending unless both
the caller and the explicit approval-auto-approve setting enable local approval.
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
    queue_counts: dict[str, int] = field(default_factory=dict)
    human_exceptions: list[dict[str, str]] = field(default_factory=list)

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
        if self.queue_counts:
            lines.append(
                "  queue          : "
                + ", ".join(
                    f"{status} {count}" for status, count in sorted(self.queue_counts.items())
                )
            )
        if self.human_exceptions:
            lines.append(f"  human exceptions: {len(self.human_exceptions)}")
        return "\n".join(lines)


def _agent_name(row: Any) -> str:
    return str(row["name"]) if row is not None else "—"


async def _ready_batch(
    session: Any, org_id: str, root_id: str, limit: int, *, exclude: set[str] | None = None
) -> list[Task]:
    """Independent tasks at the deepest ready tier, bounded by dispatch capacity.

    Deepest first, and within a depth the earliest first, so a department finishes
    before the office above it is asked to judge anything.
    """
    runnable_states = {s.value for s in RUNNABLE}
    tree = await _tree(session, org_id, root_id)
    runnable = [
        t
        for t in tree
        if (
            str(t.status) in runnable_states
            or (
                str(t.status) == TaskStatus.WAITING_FOR_APPROVAL.value
                and await _approved_continuation(session, org_id, str(t.id))
            )
        )
        and str(t.id) not in (exclude or set())
        and not await TaskRepository(session, org_id).unsatisfied_dependencies(str(t.id))
    ]
    if not runnable:
        return []
    depths = {str(t.id): _depth_of(t, root_id) for t in tree}
    deepest = max(depths[str(t.id)] for t in runnable)
    return sorted(
        (t for t in runnable if depths[str(t.id)] == deepest),
        key=lambda t: (t.created_at, _seq(t)),
    )[:limit]


async def _approved_continuation(session: Any, org: str, task_id: str) -> bool:
    status = (
        await session.execute(
            select(Approval.status)
            .where(
                Approval.organization_id == org,
                Approval.task_id == task_id,
                Approval.action_type == "task.continue",
            )
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return bool(status == "approved")


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


async def _execute_claimed_step(
    database: Database,
    organization_id: str,
    task_id: str,
    depth: int,
    *,
    run_mode: RunMode,
    auto_approve: bool,
    agent_id: str | None = None,
) -> StepLog | None:
    """One worker, one session. A concurrent driver cannot claim the same row.

    Holding the row lock through the execution keeps two bills from racing; the
    driver never holds a session while awaiting a batch of model calls.
    """
    async with database.tenant_session(organization_id) as session:
        task = (
            await session.execute(
                select(Task)
                .where(
                    Task.organization_id == organization_id,
                    Task.id == task_id,
                    Task.status.in_((*RUNNABLE, TaskStatus.WAITING_FOR_APPROVAL)),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return None
        if (
            task.status == TaskStatus.WAITING_FOR_APPROVAL.value
            and not await _approved_continuation(session, organization_id, task_id)
        ):
            return None
        owner = await _owner_name(
            session, organization_id, str(agent_id or task.owner_agent_id or "")
        )
        started = asyncio.get_running_loop().time()
        service = TaskExecutionService(
            session=session,
            organization_id=organization_id,
            run_mode=run_mode,
            auto_approve=auto_approve,
            runtime=build_runtime(),
        )
        await service.execute_task(task_id, agent_id=agent_id)
        await session.flush()
        return StepLog(
            task_id=task_id,
            title=str(task.title)[:60],
            owner=owner,
            status=str(task.status),
            depth=depth,
            seconds=asyncio.get_running_loop().time() - started,
        )


async def _execute_step(
    database: Database,
    organization_id: str,
    task_id: str,
    depth: int,
    *,
    run_mode: RunMode,
    auto_approve: bool,
    agent_id: str | None = None,
) -> StepLog | None:
    """Enforce the existing task deadline even while a provider is still awaiting I/O."""
    from ai_orchestrator.config.settings import get_settings

    timeout_s = get_settings().default_task_timeout_s
    started = asyncio.get_running_loop().time()
    timer = asyncio.timeout(timeout_s)
    logger.info("pipeline.step_started", task_id=task_id, depth=depth)
    try:
        async with timer:
            result = await _execute_claimed_step(
                database,
                organization_id,
                task_id,
                depth,
                run_mode=run_mode,
                auto_approve=auto_approve,
                agent_id=agent_id,
            )
        if result is not None:
            logger.info("pipeline.step_finished", task_id=task_id, status=result.status)
        return result
    except TimeoutError:
        if not timer.expired():
            raise
        # The interrupted transaction rolled back. Report the timeout in a
        # fresh transaction, rather than attempting to write through its corpse.
        async with database.tenant_session(organization_id) as session:
            repo = TaskRepository(session, organization_id)
            task = await repo.get(task_id)
            reason = f"the execution exceeded its configured task deadline of {timeout_s} seconds"
            await repo.transition(
                task_id, Transition.FAIL, error=reason, failure_category="timeout"
            )
            await AuditService(session, organization_id).record(
                actor=Actor(id="system:pipeline", kind=ActorType.SYSTEM),
                action="pipeline.task_timeout",
                resource_type="task",
                resource_id=task_id,
                task_id=task_id,
                outcome="failure",
                context={"reason": reason},
            )
            return StepLog(
                task_id=task_id,
                title=str(task.title)[:60],
                owner=await _owner_name(
                    session, organization_id, str(agent_id or task.owner_agent_id or "")
                ),
                status="failed",
                depth=depth,
                seconds=asyncio.get_running_loop().time() - started,
            )


async def _review_steps(
    database: Database,
    org: str,
    logs: list[StepLog],
    outcome: PipelineOutcome,
    max_attempts: int,
) -> None:
    """Review committed results once per parent; workers never share this session."""
    async with database.committing_tenant_session(org) as session:
        parents: set[str] = set()
        for log in logs:
            parent_id = (
                await session.execute(
                    select(Task.parent_task_id).where(
                        Task.organization_id == org,
                        Task.id == log.task_id,
                    )
                )
            ).scalar_one()
            if not parent_id or parent_id in parents:
                continue
            parents.add(parent_id)
            review = await review_office_work(session, org, parent_id, max_attempts=max_attempts)
            log.review = ", ".join(
                sorted({r.action for r in review.reviewed if r.action != "waiting"})
            )
            outcome.reviews_accepted += len(review.accepted)
            outcome.reviews_rerun += len(review.rerun)
            outcome.reviews_escalated += len(review.escalated)
        await session.commit()


async def run_pipeline(
    database: Database,
    organization_id: str,
    root_task_id: str,
    *,
    auto_approve: bool = True,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_executions: int = MAX_EXECUTIONS,
    concurrency: int | None = None,
    root_agent_id: str | None = None,
    run_mode: RunMode = RunMode.LIVE,
) -> PipelineOutcome:
    """Drain a goal with bounded workers, dispatching again as soon as one finishes.

    A slow office cannot hold an already assigned department behind a batch barrier.
    Reviews run serially over committed results. Settlement still runs after the
    final execution slot, so accepted work cannot be stranded at the bound.
    """
    from ai_orchestrator.config.settings import get_settings

    settings = get_settings()
    workers = settings.pipeline_concurrency if concurrency is None else concurrency
    if not 1 <= workers <= 16 or max_executions < 1:
        raise ValueError("concurrency must be 1..16 and max_executions must be positive")
    may_approve = auto_approve and settings.approval_auto_approve
    outcome = PipelineOutcome(root_task_id=root_task_id)
    approver = Actor(
        id=operator_id_for(organization_id),
        kind=ActorType.HUMAN,
        is_privileged_human=True,
        display_name="local operator (autonomous pipeline)",
    )
    active: dict[str, asyncio.Task[StepLog | None]] = {}
    claimed_elsewhere: set[str] = set()
    try:
        while True:
            async with database.committing_tenant_session(organization_id) as session:
                root = (
                    await session.execute(
                        select(Task).where(
                            Task.organization_id == organization_id,
                            Task.id == root_task_id,
                        )
                    )
                ).scalar_one_or_none()
                if root is None:
                    outcome.stopped_because = "the root task does not exist"
                    break
                outcome.root_status = str(root.status)
                tree = await _tree(session, organization_id, root_task_id)
                tree_terminal = all(TaskStatus(task.status) in TERMINAL for task in tree)
                if tree_terminal and not active:
                    retried = (
                        await _retry_a_root_that_answered_itself(
                            session,
                            organization_id,
                            root,
                            max_attempts=max_attempts,
                        )
                        if len(outcome.steps) < max_executions
                        else None
                    )
                    if retried is None:
                        break
                    outcome.root_retries += 1
                    root_task_id = retried
                    outcome.root_task_id = retried
                    await session.commit()
                    continue
                settled = await settle_finished(
                    session,
                    organization_id,
                    root_task_id,
                    max_attempts=max_attempts,
                )
                if settled.completed or settled.failed:
                    outcome.settled.extend(settled.completed)
                    outcome.settled_failed.extend(settled.failed)
                    await session.commit()
                    continue
                remaining = max_executions - len(outcome.steps) - len(active)
                capacity = min(workers - len(active), remaining)
                ready = (
                    await _ready_batch(
                        session,
                        organization_id,
                        root_task_id,
                        max(0, capacity),
                        exclude=set(active) | claimed_elsewhere,
                    )
                    if capacity > 0
                    else []
                )
                if ready:
                    for task in ready:
                        task_id = str(task.id)
                        active[task_id] = asyncio.create_task(
                            _execute_step(
                                database,
                                organization_id,
                                task_id,
                                _depth_of(task, root_task_id),
                                run_mode=run_mode,
                                auto_approve=may_approve,
                                agent_id=root_agent_id if task_id == root_task_id else None,
                            )
                        )
                    await session.commit()
                    # Re-fill capacity from the next tier before waiting: the
                    # deepest batch may be smaller than the available pool.
                    continue
                if not active:
                    if remaining <= 0:
                        outcome.stopped_because = (
                            f"the execution bound of {max_executions} was reached"
                        )
                    elif may_approve and await _clear_gates(
                        session, organization_id, root_task_id, approver
                    ):
                        await session.commit()
                        continue
                    else:
                        outcome.stopped_because = (
                            "the ready tasks are claimed by another worker"
                            if claimed_elsewhere
                            else "nothing is runnable and no gate can be cleared"
                        )
                    await session.commit()
                    break
                await session.commit()
            done, _ = await asyncio.wait(active.values(), return_when=asyncio.FIRST_COMPLETED)
            logs = []
            for task_id, future in list(active.items()):
                if future not in done:
                    continue
                del active[task_id]
                result = future.result()
                if result is None:
                    claimed_elsewhere.add(task_id)
                else:
                    logs.append(result)
            outcome.steps.extend(logs)
            await _review_steps(database, organization_id, logs, outcome, max_attempts)
    finally:
        # A cancelled driver must not return with orphaned model calls or
        # dispose the database while a sibling still uses it.
        for future in active.values():
            future.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)

    async with database.committing_tenant_session(organization_id) as session:
        await _request_failed_escalation(session, organization_id, root_task_id)
        await _collect_waiting(session, organization_id, root_task_id, outcome)
        for task in await _tree(session, organization_id, root_task_id):
            status = str(task.status)
            outcome.queue_counts[status] = outcome.queue_counts.get(status, 0) + 1
            if str(task.id) == root_task_id:
                outcome.root_status = status
        if outcome.waiting_for_human:
            outcome.stopped_because = "waiting on a person to approve a gate"
        await AuditService(session, organization_id).record(
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
                "queue_counts": outcome.queue_counts,
                "concurrency": workers,
                "stopped_because": outcome.stopped_because,
                "human_exceptions": outcome.human_exceptions,
            },
        )
        await session.commit()
    return outcome


async def _request_failed_escalation(session: Any, org: str, root_id: str) -> None:
    """A failed review budget reaches the human inbox once; approval cannot undo failure."""
    from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
    from ai_orchestrator.domain.enums import EffectClass, RiskLevel

    root = (
        await session.execute(
            select(Task).where(Task.organization_id == org, Task.id == root_id).with_for_update()
        )
    ).scalar_one_or_none()
    if root is None or root.status != "failed" or root.failure_category != "review_escalated":
        return
    if any(t.failure_category == "approval_rejected" for t in await _tree(session, org, root_id)):
        return
    existing = (
        await session.execute(
            select(Approval.id).where(
                Approval.organization_id == org,
                Approval.task_id == root_id,
                Approval.action_type == "task.escalation.review",
            )
        )
    ).first()
    if existing:
        return
    approval = await ApprovalService(session, org).create(
        ApprovalRequest(
            organization_id=org,
            action_type="task.escalation.review",
            action_payload={
                "task_id": root_id,
                "exception_class": "failed_escalation",
                "findings": root.last_error or "the organisation exhausted its review attempts",
            },
            requested_by="system:pipeline",
            requested_by_type=ActorType.SYSTEM,
            effect_class=EffectClass.PREPARE,
            risk_level=RiskLevel.HIGH,
            reason=(
                "The organisation exhausted its review attempts. A human must review the "
                "findings and decide whether to commission corrected work. "
                + (root.last_error or "")
            )[:2000],
            task_id=root_id,
            ttl_seconds=86_400,
        )
    )
    await AuditService(session, org).record(
        actor=Actor(id="system:pipeline", kind=ActorType.SYSTEM),
        action="pipeline.human_exception",
        resource_type="approval",
        resource_id=str(approval.id),
        task_id=root_id,
        context={"exception_class": "failed_escalation", "rule": "review.attempts_exhausted"},
    )


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
        input={**dict(root.input or {}), "attempt": attempt + 1},
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
    cleared = 0
    for approval in mine:
        try:
            await approvals.decide(
                str(approval.id), approver=approver, approve=True, note="autonomous pipeline"
            )
            cleared += 1
        except Exception as exc:
            logger.warning("pipeline.approval_refused", approval=approval.id, error=str(exc))
    return cleared > 0


async def _collect_waiting(
    session: Any, org_id: str, root_id: str, outcome: PipelineOutcome
) -> None:
    """Record what the run is blocked on, so "it stopped" says on what."""
    tree = await _tree(session, org_id, root_id)
    outcome.waiting_for_human = sorted(
        str(t.id) for t in tree if TaskStatus(t.status) is TaskStatus.WAITING_FOR_APPROVAL
    )
    from ai_orchestrator.domain.human_exceptions import approval_class

    for approval in (
        (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == org_id,
                    Approval.task_id.in_([str(task.id) for task in tree]),
                    Approval.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    ):
        outcome.human_exceptions.append(
            {
                "approval_id": str(approval.id),
                "task_id": str(approval.task_id),
                "class": approval_class(
                    action_type=approval.action_type,
                    effect=approval.effect_class,
                    payload=approval.action_payload or {},
                ).value,
                "reason": str(approval.reason),
            }
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
