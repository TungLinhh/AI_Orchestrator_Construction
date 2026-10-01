"""Task, delegation and event repositories.

Three things in here are load-bearing for correctness:

`create_task` writes the task and its outbox event in one transaction
    A task that exists but was never announced leaves every other component
    waiting for work that no one knows about. Conversely, publishing inside the
    transaction holds a lock across a network call. The outbox pattern avoids
    both failure modes.

`transition` goes through the state machine
    The database stores the status; the domain decides which changes are legal.
    A repository that wrote `task.status = 'completed'` directly would let a bug
    skip the approval gate.

`record_delegation` rejects a cycle before inserting
    Checked here, not only in the domain helper, because the path is persisted
    with the delegation: the audit row must show the route that was evaluated.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Select, and_, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.delegation import (
    DelegationLimits,
    DelegationPath,
    DelegationStep,
    assess_duplicate_work,
    authorize_delegation,
    task_fingerprint,
)
from ai_orchestrator.domain.enums import (
    TERMINAL_TASK_STATUSES,
    DelegationStatus,
    EventType,
    TaskStatus,
)
from ai_orchestrator.domain.errors import (
    ConflictError,
    CycleDetected,
    NotFoundError,
    PreconditionError,
    TaskConflictError,
    ValidationError,
)
from ai_orchestrator.domain.ids import (
    AgentId,
    DelegationId,
    EventId,
    ExecutionId,
    OutboxEventId,
    TaskId,
)
from ai_orchestrator.domain.state_machines import (
    Transition,
    next_delegation_status,
    next_task_status,
)
from ai_orchestrator.events.envelope import CloudEvent, subject_for
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import (
    Delegation,
    Event,
    Execution,
    OutboxEvent,
    Task,
    TaskDependency,
)

#: Only one live task per (organization, fingerprint). Enforced by a partial
#: unique index in the schema; the pre-check here exists to turn a constraint
#: violation into a comprehensible error rather than a 500.
_ACTIVE_STATUS_SQL = "status NOT IN ('completed','failed','canceled','expired')"


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


#: The partial unique index that enforces "one live copy of a piece of work".
#: Named because the error handler matches on it — a constraint that decides what the
#: platform *tells the caller* has to be identified by name, not inferred.
DEDUP_INDEX_NAME = "uq_tasks_active_dedup_key"


def _constraint_named_in(exc: IntegrityError) -> str:
    """The constraint a driver named, or `unknown`.

    Best-effort on purpose. Reading a driver's message is fragile and a driver that
    does not name the constraint yields `unknown` — which is the honest answer and is
    still better than confidently reporting the wrong one.
    """
    text = str(exc.orig)
    for name in (
        DEDUP_INDEX_NAME,
        "fk_tasks_requester_id_users",
        "fk_tasks_organization_id_organizations",
        "fk_tasks_owner_agent_id_agents",
        "fk_tasks_parent_task_id_tasks",
        "pk_tasks",
    ):
        if name in text:
            return name
    return "unknown"


class TaskRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        title: str,
        goal: str,
        task_type: str = "execution",
        requester_id: str | None = None,
        requester_type: str = "human",
        requester_agent_id: str | None = None,
        org_unit_id: str | None = None,
        parent_task_id: str | None = None,
        owner_agent_id: str | None = None,
        priority: str = "normal",
        input: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        expected_output_schema: dict[str, Any] | None = None,
        budget_limit_usd: float | None = None,
        budget_limit_tokens: int | None = None,
        deadline_at: object = None,
        allow_parallel: bool = False,
        event_id: str | None = None,
        intent_fingerprint: str | None = None,
    ) -> Task:
        if not goal.strip():
            msg = "task goal must not be empty"
            raise ValidationError(msg, details={"field": "goal"})
        if not title.strip():
            msg = "task title must not be empty"
            raise ValidationError(msg, details={"field": "title"})

        fingerprint = task_fingerprint(organization_id=self._org, task_type=task_type, goal=goal)
        # The dedup key is what the database's partial unique index applies to.
        # It equals the intent hash unless the caller explicitly demanded
        # parallel work, in which case it is salted so both rows can coexist
        # while the fact that they are the same intent stays queryable.
        # `dedup_key` is scoped to *where the work was asked for from*, not to the
        # goal alone.
        #
        # A delegated child inherits its parent's goal -- the office is handed the
        # CEO's objective and hands it on -- so a goal-only key made every chain's
        # second hop collide with its own parent. `create` then raised
        # `ConflictError`, `uq_tasks_active_dedup_key` would have refused the same
        # insert, and the delegation was reported as the platform declining to
        # delegate. Measured: CEO to office succeeded, office to department never
        # did, for any scenario. Every chain was stuck at one hop.
        #
        # Siblings are still caught, which is the case the key exists for: two
        # requests of the same work from the same parent, from the same agent or
        # two, collapse into one.
        scope = parent_task_id or "root"
        dedup_base = hashlib.sha256(f"{scope}\x1f{fingerprint}".encode()).hexdigest()
        dedup_key = f"{dedup_base}:{secrets.token_hex(6)}" if allow_parallel else dedup_base
        if not allow_parallel:
            duplicate = await self.find_active_by_dedup_key(dedup_key)
            if duplicate is not None:
                assessment = assess_duplicate_work(
                    fingerprint=fingerprint,
                    active_tasks=[(TaskId(duplicate.id), duplicate.fingerprint)],
                    allow_parallel=False,
                )
                if assessment.is_duplicate:
                    msg = (
                        f"an equivalent task is already active: {assessment.existing_task_id}. "
                        f"Reuse it, or pass allow_parallel=True to run them concurrently."
                    )
                    raise ConflictError(
                        msg,
                        details={
                            "existing_task_id": assessment.existing_task_id,
                            "fingerprint": fingerprint,
                        },
                    )

        task = Task(
            id=str(TaskId.create()),
            organization_id=self._org,
            org_unit_id=org_unit_id,
            parent_task_id=parent_task_id,
            root_task_id=parent_task_id,
            requester_id=requester_id,
            requester_type=requester_type,
            requester_agent_id=requester_agent_id,
            owner_agent_id=owner_agent_id,
            title=title.strip(),
            goal=goal.strip(),
            task_type=task_type,
            # A task created with an owner is already assigned. Making every
            # caller follow up with a separate assign turned the most common call
            # shape — "give this to that agent" — into a two-step operation, and a
            # caller who forgot the second step produced a task nobody owned.
            status=TaskStatus.ASSIGNED.value if owner_agent_id else TaskStatus.CREATED.value,
            priority=priority,
            input=input or {},
            constraints=constraints or {},
            expected_output_schema=expected_output_schema,
            fingerprint=fingerprint,
            dedup_key=dedup_key,
            intent_fingerprint=intent_fingerprint,
            budget_limit_usd=budget_limit_usd,
            budget_limit_tokens=budget_limit_tokens,
            deadline_at=deadline_at,
        )
        self._session.add(task)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Read which constraint actually fired before deciding what to report.
            #
            # This used to catch every `IntegrityError` and report "an equivalent task
            # was created concurrently", with the new task's own fingerprint in the
            # details — so the message was confident, specific, and wrong whenever
            # anything *else* violated an integrity rule. It was, in practice, always
            # wrong from the API: `requester_id` is a foreign key to `users.id`, the
            # control plane's service credential is not a user, and every task created
            # over HTTP came back as a deduplication conflict. A caller reading that
            # would conclude something was wrong with their request when the fault
            # was ours, and would go looking for a duplicate they did not create.
            #
            # So: match the constraint by name, and anything else is reported as what
            # it is. A wrong error message is worse than an ugly one, because it sends
            # the reader to the wrong place to look.
            if DEDUP_INDEX_NAME not in str(exc.orig):
                await self._session.rollback()
                msg = "the task could not be written: a database integrity rule was violated"
                raise ValidationError(
                    msg,
                    details={
                        "constraint": _constraint_named_in(exc),
                        "hint": (
                            "requester_id must reference an existing user; a service or "
                            "agent principal has no users row"
                        ),
                    },
                ) from exc
            # The partial unique index fired. Two agents asked for the same work
            # in the same transaction window, which the pre-check could not see.
            await self._session.rollback()
            msg = "an equivalent task was created concurrently"
            raise ConflictError(msg, details={"fingerprint": fingerprint}) from exc

        await self.emit(
            EventType.TASK_CREATED,
            subject=task.id,
            data={
                "task_id": task.id,
                "title": task.title,
                "task_type": task.task_type,
                "requester_type": task.requester_type,
                "fingerprint": fingerprint,
            },
            actor_id=requester_id,
        )
        return task

    async def get(self, task_id: str) -> Task:
        result = await self._session.execute(
            select(Task).where(and_(Task.id == task_id, Task.organization_id == self._org))
        )
        task = result.scalar_one_or_none()
        if task is None:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg, resource_type="task", resource_id=task_id)
        return task

    async def get_optional(self, task_id: str) -> Task | None:
        result = await self._session.execute(
            select(Task).where(and_(Task.id == task_id, Task.organization_id == self._org))
        )
        return result.scalar_one_or_none()

    async def subtree_statuses(
        self, root_id: str, *, organization_id: str | None = None
    ) -> dict[str, str]:
        """`{task_id: status}` for the whole tree under `root_id`, the root included.

        **A recursive walk, not `root_task_id`.** That column is written as
        `parent_task_id` (see `create`), so it names the *immediate* parent and is
        useless for "is anything still open below this task?" three tiers down --
        which is the question the office's completion depends on. A one-hop filter
        on it would have said an office had no children at all.

        The root is included because a caller asking about a tree needs the root's
        own status, and a walk that omits its starting point answers a different
        question than the one asked.
        """
        org = organization_id or self._org
        result = await self._session.execute(
            text(
                """
                WITH RECURSIVE tree AS (
                    SELECT id FROM tasks
                     WHERE organization_id = CAST(:o AS varchar(40))
                      AND id = CAST(:r AS varchar(40))
                    UNION ALL
                    SELECT t.id FROM tasks t
                      JOIN tree ON t.parent_task_id = tree.id
                     WHERE t.organization_id = CAST(:o AS varchar(40))
                )
                SELECT id, status FROM tasks
                 WHERE organization_id = CAST(:o AS varchar(40))
                   AND id IN (SELECT id FROM tree)
                """
            ),
            {"o": org, "r": root_id},
        )
        return {str(row[0]): str(row[1]) for row in result.all()}

    async def live_descendant_count(self, root_id: str) -> int:
        """How many tasks under this one have not reached a terminal state.

        The number that decides whether a coordination task may be `completed`.
        """
        statuses = await self.subtree_statuses(root_id)
        statuses.pop(str(root_id), None)
        terminal = {s.value for s in TERMINAL_TASK_STATUSES}
        return sum(1 for status in statuses.values() if status not in terminal)

    async def find_active_by_dedup_key(self, dedup_key: str) -> Task | None:
        """An active task already holding this dedup key.

        Matches on `dedup_key` rather than on `fingerprint`, and that is the whole
        point: `fingerprint` is the goal alone, so it cannot tell a duplicate from
        a task's own parent. The key passed in was built from the parent as well,
        so this lookup and `uq_tasks_active_dedup_key` ask the same question and
        answer it the same way -- the check in Python and the constraint in the
        database are two halves of one rule, not two rules that can disagree.
        """
        result = await self._session.execute(
            select(Task)
            .where(
                and_(
                    Task.organization_id == self._org,
                    Task.dedup_key == dedup_key,
                    Task.status.not_in([s.value for s in TERMINAL_TASK_STATUSES]),
                )
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_live_child_of(
        self, *, parent_task_id: str, owner_agent_id: str, title: str
    ) -> Task | None:
        """A child of this parent, given to this agent, with this title, still live.

        **This exists because the dedup that was supposed to stop duplicate work cannot.**
        Measured, in the development tenant: one parent task has **four** child tasks with
        the *same title*, the *same owner agent*, and the same status, three of them
        `assigned`. Three causes, all real:

        * `DelegationExecutor` kept its issued-intent fingerprints in an **in-memory set** on
          the instance, so the guard only held for the length of one run. A second run of the
          same parent started with an empty set.
        * The durable check that does exist -- `create` refusing an active task with the same
          fingerprint -- keys on the **child's own** fingerprint, which is derived from the
          objective's *wording*. The executor's intent key deliberately excludes the wording,
          so the two keys never coincide and the durable check cannot see what the intent key
          sees. The four rows above carry four different fingerprints and one title.
        * `ix_tasks_fingerprint` is a plain `CREATE INDEX`, **not unique**, so the database
          never enforced anything either. The comment in `create` that says the dedup key "is
          what the database's partial unique index applies to" describes an index that does
          not exist.

        So the guard is stated here, in one read, in the terms the business uses: *the same
        parent handed the same agent the same work twice*. Terminal children are excluded --
        finished work being asked for again is legitimate, and a guard that refused it would
        be a second bug wearing the first one's clothes.
        """
        result = await self._session.execute(
            select(Task)
            .where(
                and_(
                    Task.organization_id == self._org,
                    Task.parent_task_id == parent_task_id,
                    Task.owner_agent_id == owner_agent_id,
                    Task.title == title,
                    Task.status.not_in([s.value for s in TERMINAL_TASK_STATUSES]),
                )
            )
            .order_by(Task.created_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def all(
        self,
        *,
        status: str | None = None,
        owner_agent_id: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Task]:
        # `Any`: SQLAlchemy 2.0 parameterises `Select` on the *row* type, and
        # which columns a later `.where()` adds is not this function's concern.
        stmt: Select[Any] = select(Task).where(Task.organization_id == self._org)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if owner_agent_id is not None:
            stmt = stmt.where(Task.owner_agent_id == owner_agent_id)
        if parent_task_id is not None:
            stmt = stmt.where(Task.parent_task_id == parent_task_id)
        result = await self._session.execute(
            stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
        )
        rows: Sequence[Task] = result.scalars().all()
        return rows

    async def set_latest_summary(self, task_id: str, summary: str) -> bool:
        """Write a summary onto the task's most recent execution.

        Summaries live on `executions`, not on `tasks` -- a task has one row per
        attempt and the text belongs to the attempt. So a step that completes a
        task *outside* a run (the pipeline settling an office whose departments have
        all reported) has nowhere to put what they said unless it writes here.

        Returns whether anything was written, because "no execution row" is a real
        outcome for a task that was created and settled without ever running, and
        reporting it is better than pretending the summary was recorded.
        """
        from ai_orchestrator.persistence.models import Execution

        result = await self._session.execute(
            update(Execution)
            .where(
                and_(
                    Execution.organization_id == self._org,
                    Execution.task_id == task_id,
                )
            )
            .values(summary=summary[:4000])
            .returning(Execution.id)
        )
        return result.first() is not None

    async def transition(
        self,
        task_id: str,
        event: Transition,
        *,
        error: str | None = None,
        failure_category: str | None = None,
    ) -> Task:
        """Apply a state machine event, refusing illegal jumps.

        Uses a conditional UPDATE so a concurrent transition cannot interleave.
        Two workers completing the same task both read `running`, both compute
        `completed`, and the second write is silently lost. Including the
        expected current status in the WHERE clause makes the loser affect zero
        rows and be told so.
        """
        task = await self.get(task_id)
        current = TaskStatus(task.status)
        target = next_task_status(current, event)

        values: dict[str, object] = {"status": target.value, "updated_at": utcnow()}
        if target is TaskStatus.RUNNING and task.started_at is None:
            values["started_at"] = utcnow()
        if target in TERMINAL_TASK_STATUSES:
            values["completed_at"] = utcnow()
        if target is TaskStatus.FAILED:
            values["last_error"] = error
            values["failure_category"] = failure_category

        stmt = (
            update(Task)
            .where(
                and_(
                    Task.id == task_id,
                    Task.organization_id == self._org,
                    Task.status == current.value,
                )
            )
            .values(**values)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        # `rowcount` is what distinguishes a lost race from a successful write:
        # under row-level security a cross-tenant update returns success with
        # *zero rows affected*, so "did it raise?" is the wrong question.
        if result.rowcount == 0:
            msg = (
                f"task {task_id} changed status concurrently "
                f"(expected {current.value}, now something else); reload and retry"
            )
            raise TaskConflictError(msg, details={"task_id": task_id, "expected": current.value})

        await self._session.refresh(task)
        await self._emit_task_event(target, task)
        return task

    async def _emit_task_event(self, status: TaskStatus, task: Task) -> None:
        event_map = {
            TaskStatus.ASSIGNED: EventType.TASK_ASSIGNED,
            TaskStatus.RUNNING: EventType.TASK_STARTED,
            TaskStatus.COMPLETED: EventType.TASK_COMPLETED,
            TaskStatus.FAILED: EventType.TASK_FAILED,
            TaskStatus.CANCELED: EventType.TASK_CANCELED,
        }
        event_type = event_map.get(status)
        if event_type is None:
            await self.emit(
                EventType.TASK_UPDATED,
                subject=task.id,
                data={"task_id": task.id, "status": status.value},
            )
            return
        await self.emit(
            event_type,
            subject=task.id,
            data={"task_id": task.id, "status": status.value, "title": task.title},
        )

    async def assign(self, task_id: str, agent_id: str) -> Task:
        task = await self.get(task_id)
        task.owner_agent_id = agent_id
        task.updated_at = utcnow()
        await self._session.flush()
        return await self.transition(task_id, Transition.ASSIGN)

    async def set_output(self, task_id: str, output: dict[str, Any]) -> Task:
        task = await self.get(task_id)
        task.output = output
        task.updated_at = utcnow()
        await self._session.flush()
        return task

    async def record_spend(self, task_id: str, *, tokens: int, cost_usd: float) -> Task:
        """Accumulate spend on the task row.

        A blind increment rather than a read-modify-write: two concurrent model
        calls both adding to the same task must both be counted, and a Python
        `+=` on a value read earlier loses one of them.
        """
        result = await self._session.execute(
            update(Task)
            .where(and_(Task.id == task_id, Task.organization_id == self._org))
            .values(
                spent_tokens=Task.spent_tokens + tokens,
                spent_usd=Task.spent_usd + cost_usd,
                updated_at=utcnow(),
            )
            .returning(Task.id)
        )
        if result.scalar_one_or_none() is None:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg, resource_type="task", resource_id=task_id)
        task = await self.get(task_id)
        # The row was updated with a Core statement, so the identity-mapped
        # object still holds the pre-update values. Without this refresh the
        # caller reads stale spend and believes the budget is untouched.
        await self._session.refresh(task)
        return task

    async def renew_lease(self, task_id: str, seconds: int) -> None:
        """Extend the work lease.

        The lease is what distinguishes "slow" from "dead". Without it a crashed
        worker's task waits forever, and a system that never gives up on a task
        is a system that accumulates zombies.
        """

        await self._session.execute(
            text(
                "UPDATE tasks SET lease_expires_at = now() + make_interval(secs => :secs), "
                "updated_at = now() WHERE id = :task_id AND organization_id = :org"
            ),
            {"secs": seconds, "task_id": task_id, "org": self._org},
        )

    async def add_dependency(
        self, *, task_id: str, depends_on_task_id: str, dependency_type: str = "finish_to_start"
    ) -> TaskDependency:
        if task_id == depends_on_task_id:
            msg = "a task cannot depend on itself"
            raise ValidationError(msg, details={"task_id": task_id})
        dep = TaskDependency(
            id=str(TaskId.create()),
            organization_id=self._org,
            task_id=task_id,
            depends_on_task_id=depends_on_task_id,
            dependency_type=dependency_type,
        )
        self._session.add(dep)
        await self._session.flush()
        return dep

    async def unsatisfied_dependencies(self, task_id: str) -> Sequence[str]:
        result = await self._session.execute(
            select(TaskDependency.depends_on_task_id)
            .join(Task, Task.id == TaskDependency.depends_on_task_id)
            .where(
                and_(
                    TaskDependency.task_id == task_id,
                    TaskDependency.is_satisfied.is_(False),
                    Task.status.not_in([s.value for s in TERMINAL_TASK_STATUSES]),
                )
            )
        )
        return list(result.scalars().all())

    async def find_cycle(self, task_id: str) -> list[str] | None:
        """Is `task_id` reachable from its own dependencies?

        A cycle in the dependency graph makes every task in it wait forever. The
        graph is walked from the task's dependencies rather than trusting
        insertion order, because dependencies arrive from several agents
        concurrently and no single writer can enforce global acyclicity.

        The start node is on the stack from the beginning, so the cycle test is
        "did I arrive somewhere I already am on this path", not "am I the start".
        Treating the start as a cycle would report every task as one.
        """
        result = await self._session.execute(
            select(TaskDependency.depends_on_task_id, TaskDependency.task_id).where(
                TaskDependency.organization_id == self._org
            )
        )
        edges: dict[str, list[str]] = {}
        for depends_on, dependent in result.all():
            edges.setdefault(dependent, []).append(depends_on)

        stack: list[str] = []
        seen: set[str] = set()
        found: list[str] | None = None

        def walk(node: str) -> None:
            nonlocal found
            if found is not None:
                return
            if node in stack:
                found = [*stack[stack.index(node) :], node]
                return
            if node in seen:
                return
            seen.add(node)
            stack.append(node)
            for parent in edges.get(node, []):
                walk(parent)
            stack.pop()

        for dependency in edges.get(task_id, []):
            if found is not None:
                break
            walk(dependency)
        return found

    async def emit(
        self,
        event_type: EventType,
        *,
        subject: str,
        data: dict[str, Any],
        actor_id: str | None = None,
    ) -> Event:
        """Record an event and queue it for publication, in this transaction.

        The event row and the outbox row are written together with the caller's
        state change, so a committed change always has a corresponding event and
        an uncommitted one never does.
        """
        event = Event(
            id=str(EventId.create()),
            organization_id=self._org,
            type=event_type.value,
            source="control-plane",
            subject=subject,
            data=data,
            actor_id=actor_id,
        )
        self._session.add(event)

        envelope = CloudEvent(
            id=event.id,
            type=event_type.value,
            source="control-plane/control-plane",
            subject=subject,
            organization_id=self._org,
            data=data,
            trace_id=None,
        )
        self._session.add(
            OutboxEvent(
                id=str(OutboxEventId.create()),
                organization_id=self._org,
                event_id=event.id,
                event_type=event_type.value,
                subject=subject,
                nats_subject=subject_for(self._org, event_type),
                payload=envelope.to_dict(),
            )
        )
        await self._session.flush()
        return event

    async def count_by_status(self) -> dict[str, int]:
        result = await self._session.execute(
            select(Task.status, func.count())
            .where(Task.organization_id == self._org)
            .group_by(Task.status)
        )
        return {status: count for status, count in result.all()}

    async def stale_passive_tasks(self, *, older_than_seconds: int = 3600) -> Sequence[Task]:
        """Tasks sitting in a passive state with no progress.

        `blocked`, `waiting_for_input` and `waiting_for_approval` are all states
        where nothing happens without an external event. A task that has been in
        one for longer than the threshold has either lost its event or never had
        one, and either way an operator should see it.
        """
        result = await self._session.execute(
            select(Task).where(
                and_(
                    Task.organization_id == self._org,
                    Task.status.in_(
                        [
                            TaskStatus.BLOCKED.value,
                            TaskStatus.WAITING_FOR_INPUT.value,
                            TaskStatus.WAITING_FOR_APPROVAL.value,
                        ]
                    ),
                    Task.updated_at < text(f"now() - interval '{older_than_seconds} seconds'"),
                )
            )
        )
        return result.scalars().all()


class DelegationRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def record(
        self,
        *,
        parent_task_id: str,
        source_agent_id: str,
        target_agent_id: str,
        objective: str,
        path: DelegationPath,
        platform_limits: DelegationLimits,
        parent_limits: DelegationLimits,
        requested_limits: DelegationLimits | None = None,
        input: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        required_output_schema: dict[str, Any] | None = None,
        budget_limit_usd: float | None = None,
        budget_limit_tokens: int | None = None,
        current_fanout: int = 0,
        current_active_descendants: int = 0,
        child_task_id: str | None = None,
    ) -> Delegation:
        """Authorise and persist a delegation.

        The authorisation runs first, so a cycle is refused before a row exists.
        The verdict's reason is stored on the denial path so an auditor can see
        which limit stopped it.

        `child_task_id` is the task the work was handed to, and it is **required in
        practice** even though the column is nullable. `DelegationExecutor` creates the
        child task and then called this without it, so every delegation row the platform
        has ever written has `child_task_id = NULL` -- measured: 7 of 7.

        The column is nullable because a *refused* delegation has no child. But nothing
        here can be refused: `record` raises before it builds a row. So the null case
        means only "a caller forgot", and every reader that walks the tree has to fall
        back to `tasks.parent_task_id` and guess. `page`'s delegation graph did exactly
        that, and drew a list of boxes with no edges between them.
        """
        verdict = authorize_delegation(
            source=source_agent_id,  # type: ignore[arg-type]
            target=target_agent_id,  # type: ignore[arg-type]
            path=path,
            requested=requested_limits,
            parent_limits=parent_limits,
            platform_limits=platform_limits,
            current_fanout=current_fanout,
            current_active_descendants=current_active_descendants,
        )
        if not verdict.allowed:
            if "cycle" in verdict.reason:
                raise CycleDetected(verdict.reason, details={"path": path.to_dict()})
            msg = verdict.reason
            raise PreconditionError(msg, details={"reason": verdict.reason})

        effective = verdict.effective_limits
        delegation = Delegation(
            id=str(DelegationId.create()),
            organization_id=self._org,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            status=DelegationStatus.ACCEPTED.value,
            objective=objective,
            input=input or {},
            constraints=constraints or {},
            delegation_path=path.to_dict(),
            depth=path.depth,
            budget_limit_usd=budget_limit_usd
            if budget_limit_usd is not None
            else effective.max_cost_usd,
            budget_limit_tokens=(
                budget_limit_tokens if budget_limit_tokens is not None else effective.max_tokens
            ),
            required_output_schema=required_output_schema,
        )
        self._session.add(delegation)
        await self._session.flush()

        # A delegation is a governance-relevant action, so it belongs in the
        # audit log as well as the event stream. An event says "this happened";
        # an audit row says who asked, under which limits, along the recorded
        # route. Without it, a task's audit timeline cannot show that the work was
        # ever handed off.
        from ai_orchestrator.audit import AuditService
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType

        await AuditService(self._session, self._org).record(
            actor=Actor(id=source_agent_id, kind=ActorType.AGENT),
            action="task.delegate",
            resource_type="agent",
            resource_id=target_agent_id,
            task_id=parent_task_id,
            context={
                "delegation_id": delegation.id,
                "depth": delegation.depth,
                "objective": objective[:200],
                "path": [s["agent_id"] for s in delegation.delegation_path],
                "max_tokens": effective.max_tokens,
                "max_cost_usd": str(effective.max_cost_usd),
            },
        )

        await TaskRepository(self._session, self._org).emit(
            EventType.DELEGATION_ACCEPTED,
            subject=delegation.id,
            data={
                "delegation_id": delegation.id,
                "parent_task_id": parent_task_id,
                "source_agent_id": source_agent_id,
                "target_agent_id": target_agent_id,
                "depth": delegation.depth,
            },
        )
        return delegation

    async def get(self, delegation_id: str) -> Delegation:
        result = await self._session.execute(
            select(Delegation).where(
                and_(Delegation.id == delegation_id, Delegation.organization_id == self._org)
            )
        )
        delegation = result.scalar_one_or_none()
        if delegation is None:
            msg = f"delegation not found: {delegation_id}"
            raise NotFoundError(msg, resource_type="delegation", resource_id=delegation_id)
        return delegation

    async def transition(self, delegation_id: str, event: Transition) -> Delegation:
        delegation = await self.get(delegation_id)
        target = next_delegation_status(DelegationStatus(delegation.status), event)
        delegation.status = target.value
        if target is DelegationStatus.IN_PROGRESS and delegation.accepted_at is None:
            delegation.accepted_at = utcnow()
        if target in {
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELED,
            DelegationStatus.TIMED_OUT,
        }:
            delegation.completed_at = utcnow()
        await self._session.flush()
        return delegation

    async def active_count_for_agent(self, agent_id: str) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Delegation)
            .where(
                and_(
                    Delegation.organization_id == self._org,
                    Delegation.source_agent_id == agent_id,
                    Delegation.status.in_(
                        [
                            DelegationStatus.ACCEPTED.value,
                            DelegationStatus.IN_PROGRESS.value,
                            DelegationStatus.BLOCKED.value,
                        ]
                    ),
                )
            )
        )
        return result.scalar_one()

    async def issued_by_for_parent(self, source_agent_id: str, parent_task_id: str) -> int:
        """How many delegations this agent has already issued from this task.

        The fan-out cap's numerator. Counted by *issued*, not by active status,
        because a delegation that was refused and then retried must still count
        against the cap — otherwise an agent that keeps hitting refusals gets
        unlimited attempts, which is the opposite of what a cap is for.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(Delegation)
            .where(
                and_(
                    Delegation.organization_id == self._org,
                    Delegation.source_agent_id == source_agent_id,
                    Delegation.parent_task_id == parent_task_id,
                )
            )
        )
        return int(result.scalar() or 0)

    async def issued_by(self, source_agent_id: str) -> int:
        """Every delegation this agent has issued, at any depth.

        The active-descendant cap's numerator.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(Delegation)
            .where(
                and_(
                    Delegation.organization_id == self._org,
                    Delegation.source_agent_id == source_agent_id,
                )
            )
        )
        return int(result.scalar() or 0)

    async def list_for_task(self, task_id: str) -> Sequence[Delegation]:
        result = await self._session.execute(
            select(Delegation)
            .where(
                and_(
                    Delegation.organization_id == self._org,
                    Delegation.parent_task_id == task_id,
                )
            )
            .order_by(Delegation.created_at)
        )
        return result.scalars().all()

    async def load_path(self, delegation_id: str) -> DelegationPath:
        """Rebuild the ancestor chain from a stored delegation.

        Used when a delegated task spawns its own sub-delegation: the new
        delegation must extend the *original* path, not start a fresh one, or the
        cycle check would be blind to everything above it.
        """
        delegation = await self.get(delegation_id)
        return DelegationPath(
            steps=[
                DelegationStep(
                    agent_id=AgentId(entry["agent_id"]),
                    task_id=TaskId(entry["task_id"]),
                    depth=entry["depth"],
                )
                for entry in delegation.delegation_path
            ]
        )


class ExecutionRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def record_call_counts(
        self, execution_id: str, *, tool_calls: int, model_calls: int
    ) -> None:
        """Attach this run's call counts to its execution. Called once, at the end.

        Not a per-call increment, on purpose -- see migration 0029. The write
        happens inside the run's own transaction, so it commits or rolls back with
        the run it describes and can never disagree with the outcome.
        """
        result = await self._session.execute(
            update(Execution)
            .where(
                Execution.id == execution_id,
                Execution.organization_id == self._org,
            )
            .values(tool_call_count=tool_calls, model_call_count=model_calls)
        )
        from sqlalchemy.engine import CursorResult

        if isinstance(result, CursorResult) and result.rowcount == 0:
            msg = f"execution {execution_id} was not found in this organisation"
            raise NotFoundError(msg, resource_type="execution", resource_id=execution_id)

    async def start(
        self,
        *,
        task_id: str,
        agent_id: str | None,
        runtime_adapter: str,
        model_profile: str,
        attempt: int = 1,
        delegation_id: str | None = None,
        input_hash: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
    ) -> Execution:
        execution = Execution(
            id=str(ExecutionId.create()),
            organization_id=self._org,
            task_id=task_id,
            agent_id=agent_id,
            delegation_id=delegation_id,
            attempt=attempt,
            status="running",
            runtime_adapter=runtime_adapter,
            model_profile=model_profile,
            input_hash=input_hash or _hash_payload({"task_id": task_id}),
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
        )
        self._session.add(execution)
        await self._session.flush()
        return execution

    async def get(self, execution_id: str) -> Execution:
        result = await self._session.execute(
            select(Execution).where(
                and_(Execution.id == execution_id, Execution.organization_id == self._org)
            )
        )
        execution = result.scalar_one_or_none()
        if execution is None:
            msg = f"execution not found: {execution_id}"
            raise NotFoundError(msg, resource_type="execution", resource_id=execution_id)
        return execution

    async def finish(
        self,
        execution_id: str,
        *,
        status: str,
        summary: str = "",
        decision_record: dict[str, Any] | None = None,
        artifacts: list[Any] | None = None,
        escalation: dict[str, Any] | None = None,
        error_kind: str | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        model_used: str | None = None,
        output_hash: str | None = None,
        retry_count: int = 0,
    ) -> Execution:
        execution = await self.get(execution_id)
        execution.status = status
        execution.summary = summary
        execution.decision_record = decision_record or {}
        execution.artifacts = artifacts or []
        execution.escalation = escalation
        execution.error_kind = error_kind
        execution.error_category = error_category
        execution.error_message = error_message
        execution.input_tokens = input_tokens
        execution.output_tokens = output_tokens
        execution.reasoning_tokens = reasoning_tokens
        execution.cost_usd = execution.cost_usd + cost_usd
        execution.duration_ms = duration_ms
        execution.retry_count = retry_count
        execution.finished_at = utcnow()
        if model_used:
            execution.model_used = model_used
        if output_hash:
            execution.output_hash = output_hash
        await self._session.flush()
        return execution

    async def list_for_task(self, task_id: str) -> Sequence[Execution]:
        result = await self._session.execute(
            select(Execution)
            .where(and_(Execution.organization_id == self._org, Execution.task_id == task_id))
            .order_by(Execution.started_at)
        )
        return result.scalars().all()

    async def record_retry(self, execution_id: str) -> None:
        await self._session.execute(
            update(Execution)
            .where(and_(Execution.id == execution_id, Execution.organization_id == self._org))
            .values(retry_count=Execution.retry_count + 1)
        )

    async def append_decision(self, execution_id: str, entry: dict[str, Any]) -> None:
        """Append to the decision record.

        An append, not a replace: the record has to show the sequence of choices
        that led to the outcome, not just the last one.
        """
        execution = await self.get(execution_id)
        record = list(execution.decision_record.get("steps", []))
        record.append(entry)
        execution.decision_record = {"steps": record}
        await self._session.flush()


__all__ = [
    "DelegationRepository",
    "ExecutionRepository",
    "TaskRepository",
]
