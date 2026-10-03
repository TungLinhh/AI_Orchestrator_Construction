"""Task lifecycle, delegation and duplicate detection, against a real database.

These cover the parts of the platform where a bug is expensive: a task that
skips an approval gate, two agents doing the same work, or a delegation that
loops forever. The state machine itself is unit-tested elsewhere; what is proved
here is that the repository, the database constraints and the transactional
outbox actually enforce it.

Everything runs through `tenant`, the same `tenant_session` path the
application uses, so the RLS tenant binding under test is the production one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from ai_orchestrator.domain.delegation import (
    DelegationLimits,
    DelegationPath,
    intent_fingerprint,
)
from ai_orchestrator.domain.enums import EventType, TaskStatus
from ai_orchestrator.domain.errors import (
    ConflictError,
    CycleDetected,
    PreconditionError,
    TaskConflictError,
    ValidationError,
)
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Organization
from ai_orchestrator.persistence.repositories.organization import (
    AgentRepository,
    RoleRepository,
)
from ai_orchestrator.persistence.repositories.task import (
    DelegationRepository,
    ExecutionRepository,
    TaskRepository,
)
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class TestTaskLifecycle:
    async def test_task_starts_in_created_with_a_fingerprint(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="Analyse revenue", goal="analyse revenue for Q3")
        assert task.status == TaskStatus.CREATED.value
        assert task.fingerprint, "every task carries a dedup fingerprint"

    async def test_happy_path(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="g")
        await repo.transition(task.id, Transition.ASSIGN)
        await repo.transition(task.id, Transition.BEGIN_WORK)
        await repo.transition(task.id, Transition.COMPLETE)
        assert (await repo.get(task.id)).status == TaskStatus.COMPLETED.value

    async def test_completed_task_cannot_be_reopened(self, tenant) -> None:
        """A finished task is history. Re-running creates a new task, because
        mutating the row would make the audit trail lie about what happened."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="g")
        await repo.transition(task.id, Transition.ASSIGN)
        await repo.transition(task.id, Transition.BEGIN_WORK)
        await repo.transition(task.id, Transition.COMPLETE)
        with pytest.raises(PreconditionError):
            await repo.transition(task.id, Transition.BEGIN_WORK)

    async def test_racing_writer_is_told_rather_than_overwriting(self, tenant, db) -> None:
        """Two workers racing on one task must not silently lose an update.

        A reads the task, B completes it on another connection, then A tries to
        write. A must be *told* — and the completion must survive. Which exception
        A gets depends on how far the race got, and both outcomes are correct:
        the state machine refuses an illegal jump, and the conditional UPDATE
        refuses a stale write. What must never happen is a silent overwrite, so
        the assertion is on the data, with either error accepted.
        """
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="racy goal")
        task_id = task.id
        await repo.transition(task_id, Transition.ASSIGN)
        await repo.transition(task_id, Transition.BEGIN_WORK)
        await tenant.commit()

        async with db.tenant_session(tenant.organization_id) as other:
            other_repo = TaskRepository(other, tenant.organization_id)
            await other_repo.transition(task_id, Transition.COMPLETE)

        with pytest.raises((TaskConflictError, PreconditionError)):
            await repo.transition(task_id, Transition.FAIL)

        assert (await repo.get(task_id)).status == TaskStatus.COMPLETED.value, (
            "the winning write must survive the race"
        )

    async def test_stale_update_predicate_rejects_a_lost_race(self, tenant, db) -> None:
        """The conditional UPDATE is the narrow-window guard.

        The state machine catches a stale *read*; this catches a write that
        becomes stale between the read and the write. It is tested directly
        because provoking that window through the public API would need timing
        the test cannot rely on — and a flaky test for a data-integrity property
        is worse than a direct one.
        """
        from sqlalchemy import and_, update

        from ai_orchestrator.persistence.models import Task as TaskRow

        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="guard goal")
        task_id = task.id
        await repo.transition(task_id, Transition.ASSIGN)
        await repo.transition(task_id, Transition.BEGIN_WORK)
        await tenant.commit()

        # Another connection moves the task on.
        async with db.tenant_session(tenant.organization_id) as other:
            other_repo = TaskRepository(other, tenant.organization_id)
            await other_repo.transition(task_id, Transition.COMPLETE)

        # This session still believes the task is `running`. Its UPDATE carries
        # the expected status, so it matches nothing.
        result = await tenant.session.execute(
            update(TaskRow)
            .where(
                and_(
                    TaskRow.id == task_id,
                    TaskRow.organization_id == tenant.organization_id,
                    TaskRow.status == TaskStatus.RUNNING.value,
                )
            )
            .values(status=TaskStatus.FAILED.value)
        )
        assert result.rowcount == 0, "a stale UPDATE must affect no rows"
        assert (await repo.get(task_id)).status == TaskStatus.COMPLETED.value

    async def test_creation_writes_an_outbox_event_in_the_same_transaction(self, tenant) -> None:
        """A committed task is always announced; an uncommitted one never is."""

        from ai_orchestrator.persistence.models import OutboxEvent

        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="Announce me", goal="announce me")
        await tenant.session.flush()

        result = await tenant.session.execute(
            select(OutboxEvent).where(
                OutboxEvent.organization_id == tenant.organization_id,
                OutboxEvent.event_type == EventType.TASK_CREATED.value,
                OutboxEvent.subject == task.id,
            )
        )
        row = result.scalar_one()
        assert row.nats_subject == f"ao.{tenant.organization_id}.task.created"
        assert row.published_at is None, "unpublished until the relay runs"

    async def test_rolled_back_task_publishes_nothing(self, tenant) -> None:
        """The outbox row is written in the same transaction as the task, so a
        rollback takes the event with it.

        Publishing inside the business transaction would announce work that then
        rolls back, and every consumer would chase a task that does not exist.
        A savepoint is used so the test can prove the rollback without tearing
        down the fixture's transaction.
        """
        from sqlalchemy import func, select

        from ai_orchestrator.persistence.models import OutboxEvent

        repo = TaskRepository(tenant.session, tenant.organization_id)

        def _count() -> object:
            return (
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.organization_id == tenant.organization_id)
            )

        before = await tenant.session.scalar(_count())

        savepoint = await tenant.session.begin_nested()
        await repo.create(title="Doomed", goal="doomed task")
        await tenant.session.flush()
        assert await tenant.session.scalar(_count()) == before + 1
        await savepoint.rollback()

        assert await tenant.session.scalar(_count()) == before, (
            "a rolled-back task must leave no outbox row"
        )

    async def test_state_changes_publish_the_matching_event_types(self, tenant) -> None:
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Event

        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="g")
        await repo.transition(task.id, Transition.ASSIGN)
        await repo.transition(task.id, Transition.BEGIN_WORK)
        await repo.transition(task.id, Transition.COMPLETE)
        await tenant.session.flush()

        result = await tenant.session.execute(
            select(Event.type).where(
                Event.organization_id == tenant.organization_id, Event.subject == task.id
            )
        )
        published = set(result.scalars())
        assert {
            EventType.TASK_CREATED.value,
            EventType.TASK_ASSIGNED.value,
            EventType.TASK_STARTED.value,
            EventType.TASK_COMPLETED.value,
        } <= published

    async def test_spend_accumulates(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="g")
        await repo.record_spend(task.id, tokens=100, cost_usd=0.01)
        await repo.record_spend(task.id, tokens=250, cost_usd=0.02)
        await tenant.session.flush()
        reloaded = await repo.get(task.id)
        assert reloaded.spent_tokens == 350
        assert float(reloaded.spent_usd) == pytest.approx(0.03)

    async def test_task_from_another_tenant_is_invisible(self, tenant) -> None:
        """A task id from another organization must not resolve, even when the
        caller knows the exact id."""
        from ai_orchestrator.domain.errors import NotFoundError

        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="T", goal="g")
        repo_other = TaskRepository(tenant.session, "org_someone_else")
        # Same session, different claimed tenant. The query predicate and RLS
        # both refuse.
        with pytest.raises(NotFoundError):
            await repo_other.get(task.id)


async def _start_running(tenant, task_id: str, agent_id: str, age: str = "now()") -> None:
    """Put a task into the one state that occupies delegation capacity: being worked on.

    `age` is a SQL interval expression, so a run three hours old -- the shape the
    console already draws as a stranded box -- is the same code path with a different
    argument rather than a second fixture.
    """
    repo = TaskRepository(tenant.session, tenant.organization_id)
    await repo.transition(task_id, Transition.ASSIGN)
    await repo.transition(task_id, Transition.BEGIN_WORK)
    await tenant.session.execute(
        text(
            "INSERT INTO executions (id, organization_id, task_id, agent_id, status,"
            f" started_at) VALUES (gen_random_uuid()::varchar, :o, :t, :a, 'running',"
            f" {age})"
        ),
        {"o": str(tenant.organization_id), "t": task_id, "a": agent_id},
    )


async def _agent_ids(session, organization_id: str, count: int) -> list[str]:
    """`count` distinct agent ids in this tenant, seeding the roster if needed.

    Distinct, because `DelegationRepository.record` refuses an agent delegating to
    itself -- a constraint that is correct and is not what the caller is testing.
    """
    found = [
        str(row)
        for row in (
            await session.execute(
                select(Agent.id).where(Agent.organization_id == organization_id).limit(count)
            )
        ).scalars()
    ]
    if len(found) < count:
        org = (
            await session.execute(select(Organization).where(Organization.id == organization_id))
        ).scalar_one()
        await seed(session, into=org)
        found = [
            str(row)
            for row in (
                await session.execute(
                    select(Agent.id).where(Agent.organization_id == organization_id).limit(count)
                )
            ).scalars()
        ]
    return found[:count]


async def _any_agent_id(session, organization_id: str) -> str:
    """The id of some agent in this tenant, seeding the roster if it is empty."""
    found = (
        await session.execute(
            select(Agent.id).where(Agent.organization_id == organization_id).limit(1)
        )
    ).scalar_one_or_none()
    if found:
        return str(found)
    org = (
        await session.execute(select(Organization).where(Organization.id == organization_id))
    ).scalar_one()
    await seed(session, into=org)
    return str(
        (
            await session.execute(
                select(Agent.id).where(Agent.organization_id == organization_id).limit(1)
            )
        ).scalar_one()
    )


class TestDuplicateWork:
    async def test_equivalent_goal_is_rejected(self, tenant) -> None:
        """Two agents describing the same job differently must collide."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        first = await repo.create(title="Market analysis", goal="Prepare a market analysis.")
        with pytest.raises(ConflictError) as exc:
            await repo.create(title="Again", goal="prepare the MARKET analysis")
        assert str(first.id) in str(exc.value.message)

    async def test_a_refused_duplicate_leaves_the_callers_work_intact(self, tenant) -> None:
        """**The refusal has to leave the caller able to keep working.**

        This is the assertion that was missing, and its absence is why the demo script
        died on `main`. The duplicate was refused correctly and the caller caught
        `ConflictError` — and the run still ended:

            asyncpg.UniqueViolationError: duplicate key ... uq_tasks_live_intent
            InvalidRequestError: Can't operate on closed transaction inside context
            manager. The transaction was rolled back due to an exception

        Because the error came from the database inside the *caller's* transaction, and
        a transaction that has seen an error is dead, "refuse it and carry on" was
        impossible. The repository used to roll the whole outer transaction back to
        clean up, which undid work that had nothing to do with the duplicate.

        So: work done before the refused duplicate must still be readable afterwards.
        That is what the savepoint buys, and it is the difference between a refusal and
        an outage.
        """
        repo = TaskRepository(tenant.session, tenant.organization_id)
        keeper = await repo.create(title="The real work", goal="do the thing that matters")

        # **An owner is required, and that is a detail worth knowing.** The index is on
        # `(organization_id, owner_agent_id, intent_fingerprint)`, and Postgres treats
        # NULLs as distinct in a unique index -- so two ownerless tasks with the same
        # intent do *not* collide. A first version of this test created both tasks
        # without an owner, raised nothing, and would have passed while proving
        # nothing: the index was never consulted.
        #
        # `intent_fingerprint` with `allow_parallel=True` is the shape the delegation
        # executor sends, and it is what reaches the *index*: the ordinary duplicate
        # check refuses earlier, in Python, on the goal text.
        owner = str(await _any_agent_id(tenant.session, str(tenant.organization_id)))
        intent = "a" * 64
        await repo.create(
            title="Same thing again",
            goal="do the thing that matters",
            owner_agent_id=owner,
            allow_parallel=True,
            intent_fingerprint=intent,
        )

        with pytest.raises(ConflictError):
            await repo.create(
                title="And again",
                goal="do the thing that matters",
                owner_agent_id=owner,
                allow_parallel=True,
                intent_fingerprint=intent,
            )

        # The session is still usable, and the work done before the refused insert is
        # still there. Reading it back is the whole assertion: on the old code this
        # line raises `InvalidRequestError` rather than returning a row.
        reloaded = await repo.get(keeper.id)
        assert reloaded.title == "The real work"

    async def test_a_refused_duplicate_does_not_undo_an_earlier_duplicate_check(
        self, tenant
    ) -> None:
        """Two refusals in a row, then real work — the shape a model actually produces.

        A model that delegates the same thing three times must see three refusals and
        then carry on, not one refusal and a dead session. Each refusal has to leave
        the transaction exactly as usable as the first one did.
        """
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="A", goal="identical work")

        for _ in range(2):
            with pytest.raises(ConflictError):
                await repo.create(title="B", goal="identical work")

        after = await repo.create(title="C", goal="different work entirely")
        assert after.id != ""

    async def test_parallel_work_must_be_requested_explicitly(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="A", goal="same work")
        second = await repo.create(title="B", goal="same work", allow_parallel=True)
        assert second.fingerprint is not None

    async def test_finished_task_does_not_block_a_new_one(self, tenant) -> None:
        """Re-running the same job later is legitimate. Dedup is about concurrent
        duplicates, not about remembering forever."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        first = await repo.create(title="A", goal="reusable job")
        await repo.transition(first.id, Transition.ASSIGN)
        await repo.transition(first.id, Transition.BEGIN_WORK)
        await repo.transition(first.id, Transition.COMPLETE)
        second = await repo.create(title="A again", goal="reusable job")
        assert second.id != first.id

    async def test_different_goals_are_independent(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="A", goal="analyse revenue")
        await repo.create(title="B", goal="audit the ledger")


class TestDependencies:
    async def test_self_dependency_rejected(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        t = await repo.create(title="T", goal="g")
        with pytest.raises(ValidationError):
            await repo.add_dependency(task_id=t.id, depends_on_task_id=t.id)

    async def test_dependency_cycle_is_detected(self, tenant) -> None:
        """A dependency cycle makes both tasks wait forever. The graph is walked
        when the edge is added, not discovered at run time."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        a = await repo.create(title="A", goal="goal a")
        b = await repo.create(title="B", goal="goal b")
        c = await repo.create(title="C", goal="goal c")
        await repo.add_dependency(task_id=a.id, depends_on_task_id=b.id)
        await repo.add_dependency(task_id=b.id, depends_on_task_id=c.id)
        await repo.add_dependency(task_id=c.id, depends_on_task_id=a.id)
        assert await repo.find_cycle(c.id) is not None

    async def test_acyclic_chain_reports_no_cycle(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        a = await repo.create(title="A", goal="goal a")
        b = await repo.create(title="B", goal="goal b")
        await repo.add_dependency(task_id=a.id, depends_on_task_id=b.id)
        assert await repo.find_cycle(a.id) is None


class TestDelegation:
    async def _agents(self, tenant):
        """Three delegating agents, each with a real definition.

        An agent without a definition has no system instructions and nothing to
        reproduce later, so the foreign key is enforced rather than optional.
        """
        from ai_orchestrator.persistence.repositories.organization import (
            AgentDefinitionRepository,
        )

        roles = RoleRepository(tenant.session, tenant.organization_id)
        role = await roles.create(name="Worker", may_delegate_to_peers=True)
        definitions = AgentDefinitionRepository(tenant.session, tenant.organization_id)
        agents = AgentRepository(tenant.session, tenant.organization_id)
        created = []
        for name in ("A", "B", "C"):
            definition = await definitions.create(
                name=f"{name}-definition", role_id=role.id, system_instructions=f"You are {name}."
            )
            created.append(
                await agents.create(name=name, role_id=role.id, definition_id=definition.id)
            )
        return created[0], created[1], created[2]

    async def test_valid_delegation_records_its_path(self, tenant) -> None:
        a, b, _c = await self._agents(tenant)
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(title="Parent", goal="parent goal")
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        limits = DelegationLimits.platform_default()
        d = await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=a.id,
            target_agent_id=b.id,
            objective="do the sub work",
            path=DelegationPath.root(a.id, parent.id),
            platform_limits=limits,
            parent_limits=limits,
        )
        assert d.status == "accepted"
        assert len(d.delegation_path) == 1, "the evaluated path is stored for audit"
        assert d.depth == 1

    async def test_circular_delegation_is_blocked(self, tenant) -> None:
        """Acceptance scenario 5: A -> B -> C -> A."""
        a, b, c = await self._agents(tenant)
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(title="Parent", goal="parent goal")
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        limits = DelegationLimits.platform_default()

        await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=a.id,
            target_agent_id=b.id,
            objective="b work",
            path=DelegationPath.root(a.id, parent.id),
            platform_limits=limits,
            parent_limits=limits,
        )
        path_bc = DelegationPath.root(a.id, parent.id).extend(b.id, parent.id)
        await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=b.id,
            target_agent_id=c.id,
            objective="c work",
            path=path_bc,
            platform_limits=limits,
            parent_limits=limits,
        )
        path_abc = path_bc.extend(c.id, parent.id)
        with pytest.raises(CycleDetected) as exc:
            await delegations.record(
                parent_task_id=parent.id,
                source_agent_id=c.id,
                target_agent_id=a.id,
                objective="loop back",
                path=path_abc,
                platform_limits=limits,
                parent_limits=limits,
            )
        assert str(a.id) in str(exc.value.message), "the denial names the closing agent"

    async def test_depth_limit_blocks_a_deep_chain(self, tenant) -> None:
        a, b, _c = await self._agents(tenant)
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(title="Parent", goal="parent goal")
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        shallow = DelegationLimits(
            max_depth=1,
            max_fanout=8,
            max_active_descendants=16,
            max_tokens=1000,
            max_cost_usd=1.0,
            max_runtime_s=60,
        )
        with pytest.raises(PreconditionError):
            await delegations.record(
                parent_task_id=parent.id,
                source_agent_id=a.id,
                target_agent_id=b.id,
                objective="too deep",
                path=DelegationPath.root(a.id, parent.id),
                platform_limits=shallow,
                parent_limits=shallow,
            )

    async def test_path_reloaded_from_storage_keeps_its_ancestors(self, tenant) -> None:
        """A grandchild delegation must extend the *stored* path. Starting fresh
        would make the cycle check blind to everything above it."""
        a, b, _c = await self._agents(tenant)
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(title="Parent", goal="parent goal")
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        limits = DelegationLimits.platform_default()
        d_ab = await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=a.id,
            target_agent_id=b.id,
            objective="b",
            path=DelegationPath.root(a.id, parent.id),
            platform_limits=limits,
            parent_limits=limits,
        )
        reloaded = await delegations.load_path(d_ab.id)
        assert reloaded.agent_ids == (a.id,)
        with pytest.raises(CycleDetected):
            await delegations.record(
                parent_task_id=parent.id,
                source_agent_id=b.id,
                target_agent_id=a.id,
                objective="back to a",
                path=reloaded.extend(b.id, parent.id),
                platform_limits=limits,
                parent_limits=limits,
            )

    async def test_child_budget_cannot_exceed_the_parent(self, tenant) -> None:
        a, b, _c = await self._agents(tenant)
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(title="Parent", goal="parent goal")
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        generous = DelegationLimits(
            max_depth=4,
            max_fanout=8,
            max_active_descendants=16,
            max_tokens=10_000_000,
            max_cost_usd=9999.0,
            max_runtime_s=3600,
        )
        stingy = DelegationLimits(
            max_depth=4,
            max_fanout=8,
            max_active_descendants=16,
            max_tokens=5_000,
            max_cost_usd=1.0,
            max_runtime_s=60,
        )
        d = await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=a.id,
            target_agent_id=b.id,
            objective="try to grab budget",
            path=DelegationPath.root(a.id, parent.id),
            platform_limits=generous,
            parent_limits=stingy,
            requested_limits=generous,
        )
        assert d.budget_limit_tokens == 5_000, "the child's envelope is clamped to the parent"
        assert float(d.budget_limit_usd) == pytest.approx(1.0)


class TestExecutions:
    async def test_execution_records_reproducibility_stamps(self, tenant) -> None:
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        task = await tasks.create(title="T", goal="g")
        executions = ExecutionRepository(tenant.session, tenant.organization_id)
        ex = await executions.start(
            task_id=task.id, agent_id=None, runtime_adapter="fake", model_profile="default"
        )
        assert ex.input_hash, "an execution must carry the input hash"
        assert ex.agent_definition_version == 1
        done = await executions.finish(ex.id, status="completed", summary="did it")
        assert done.status == "completed"
        assert done.finished_at is not None

    async def test_decision_record_accumulates(self, tenant) -> None:
        """The record is the sequence of choices, not the last one. Replacing it
        would erase the reasoning that led to the outcome."""
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        task = await tasks.create(title="T", goal="g")
        executions = ExecutionRepository(tenant.session, tenant.organization_id)
        ex = await executions.start(
            task_id=task.id, agent_id=None, runtime_adapter="fake", model_profile="default"
        )
        await executions.append_decision(ex.id, {"action": "call tool", "tool": "search"})
        await executions.append_decision(ex.id, {"action": "report", "result": "done"})
        steps = (await executions.get(ex.id)).decision_record["steps"]
        assert len(steps) == 2
        assert steps[0]["action"] == "call tool"
        assert steps[1]["result"] == "done"

    async def test_execution_list_is_ordered_and_tenant_scoped(self, tenant) -> None:
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        task = await tasks.create(title="T", goal="g")
        executions = ExecutionRepository(tenant.session, tenant.organization_id)
        for i in range(3):
            await executions.start(
                task_id=task.id,
                agent_id=None,
                runtime_adapter="fake",
                model_profile="default",
                attempt=i + 1,
            )
        found = await executions.list_for_task(task.id)
        assert len(found) == 3
        assert [e.attempt for e in found] == [1, 2, 3]


class TestTheActiveDescendantCapCountsLiveWork:
    """A finished run must not consume the organisation's delegation capacity.

    **Found by running the first real goal of the day.** The chief was refused with

        active descendants 16 reached the limit of 16

    having recorded 26 delegations across earlier runs, every one of them finished.
    The parameter was named `current_active_descendants`, the field was
    `max_active_descendants`, and the query counted all time -- so it was an all-time
    cap on how many times the chief could ever delegate, and the organisation was
    permanently unable to delegate on that tenant.

    The failure it produced was `budget_error` -- "the agent exceeded its turn
    budget" -- because the agent kept retrying a refusal the platform was certain to
    repeat. So the symptom named the wrong mechanism, which is why these tests assert
    the *count* and not the outcome.
    """

    async def _two_agents(self, tenant) -> tuple[str, str]:
        """A delegator and a distinct target: `record` refuses self-delegation, and
        rightly so -- the constraint is not what is under test here."""
        ids = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        return ids[0], ids[1]

    async def _delegate_once(self, tenant, source: str, target: str, goal: str) -> str:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        # A real parent: `delegations.parent_task_id` has a foreign key, and a made-up
        # id is the kind of shortcut that passes until the constraint notices.
        parent = await repo.create(
            title="the goal", goal="hand this on", owner_agent_id=source, allow_parallel=True
        )
        child = await repo.create(
            title=goal[:60],
            goal=goal,
            owner_agent_id=target,
            parent_task_id=parent.id,
            allow_parallel=True,
        )
        await delegations.record(
            parent_task_id=parent.id,
            child_task_id=child.id,
            source_agent_id=source,
            target_agent_id=target,
            objective=goal,
            platform_limits=DelegationLimits.platform_default(),
            path=DelegationPath(),
            parent_limits=DelegationLimits.platform_default(),
        )
        return child.id

    async def _finish(self, tenant, task_id: str) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        status = await repo.get(task_id)
        if status.status != TaskStatus.RUNNING.value:
            await repo.transition(task_id, Transition.ASSIGN)
            await repo.transition(task_id, Transition.BEGIN_WORK)
        await repo.transition(task_id, Transition.COMPLETE)

    async def test_finished_delegations_do_not_count(self, tenant) -> None:
        source, target = await self._two_agents(tenant)
        for n in range(3):
            child = await self._delegate_once(tenant, source, target, f"piece of work {n}")
            await self._finish(tenant, child)

        counted = await DelegationRepository(tenant.session, tenant.organization_id).issued_by(
            source
        )
        assert counted == 0, (
            f"{counted} finished delegation(s) still consume the budget, so the "
            "organisation runs out of capacity permanently rather than under load"
        )

    async def test_work_in_flight_still_counts(self, tenant) -> None:
        """Otherwise the fix removed the control rather than correcting it.

        **In flight means in flight**: a `running` task with an execution started inside
        the window. A *queued* task -- `created`, `assigned`, nothing running on it --
        does not count, and
        `TestAbandonedWorkDoesNotHoldTheBudget::test_a_never_started_task_does_not_count`
        is the other half of this same rule. The two were written as if they disagreed
        and the measurement decided it: 151 refusals against sixteen abandoned children.
        """
        source, target = await self._two_agents(tenant)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        child = await self._delegate_once(tenant, source, target, "still being worked")
        await _start_running(tenant, child, target)
        counted = await delegations.issued_by(source)
        assert counted == 1, (
            "the cap has stopped bounding concurrent fan-out, which is the runaway it "
            "exists to prevent"
        )

    async def test_the_same_row_stops_counting_when_it_terminates(self, tenant) -> None:
        """A cap that only grows never recovers; one that only shrinks never binds.

        The row has to start in flight — capacity is released by *finishing* work, and
        under the abandoned-work rule (F249's successor) a queued task never occupied
        anything to release.
        """
        source, target = await self._two_agents(tenant)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        child = await self._delegate_once(tenant, source, target, "one piece of work")
        await _start_running(tenant, child, target)

        assert await delegations.issued_by(source) == 1
        await self._finish(tenant, child)
        assert await delegations.issued_by(source) == 0


class TestAbandonedWorkDoesNotHoldTheBudget:
    """Capacity consumed by work that no longer exists is capacity that is gone.

    **Measured on a real run, immediately after F249.** F249 made the count exclude
    *finished* children, which was right and not sufficient. The next real procurement
    run reported

        refused_by_platform reason='active descendants 16 reached the limit of 16'
        ... 151 times, delegation.applied 0

    Sixteen children still `created` or `assigned` from runs that were killed
    mid-flight. Nothing would ever finish them and nothing would ever release them, so
    the delegation budget was fully consumed by work that no longer existed.

    The discriminator is the one the product already uses to draw a box with a stranded
    run on it: no execution started inside `STUCK_AFTER_SECONDS` means nobody is
    working on it.
    """

    async def _child(self, tenant, owner: str, goal: str):
        repo = TaskRepository(tenant.session, tenant.organization_id)
        return str(
            (
                await repo.create(
                    title=goal[:60],
                    goal=goal,
                    owner_agent_id=owner,
                    allow_parallel=True,
                )
            ).id
        )

    async def _record(self, tenant, source: str, target: str, child_id: str, goal: str) -> None:
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        parent = await self._child(tenant, source, f"the goal that sent {goal}")
        await delegations.record(
            parent_task_id=parent,
            child_task_id=child_id,
            source_agent_id=source,
            target_agent_id=target,
            objective=goal,
            platform_limits=DelegationLimits.platform_default(),
            path=DelegationPath(),
            parent_limits=DelegationLimits.platform_default(),
        )

    async def test_a_never_started_task_does_not_count(self, tenant) -> None:
        """The exact shape that consumed the whole budget: assigned, never run."""
        ids = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        source, target = ids
        child = await self._child(tenant, target, "work that was abandoned")
        await self._record(tenant, source, target, child, "the abandoned one")

        counted = await DelegationRepository(tenant.session, tenant.organization_id).issued_by(
            source
        )
        assert counted == 0, (
            f"{counted} never-started task(s) still hold the delegation budget, so the "
            "organisation cannot delegate again on this tenant"
        )

    async def test_a_task_with_a_recent_running_execution_still_counts(self, tenant) -> None:
        """Otherwise the fix would have removed the control instead of correcting it."""
        ids = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        source, target = ids
        child = await self._child(tenant, target, "work in flight")
        await self._record(tenant, source, target, child, "the in-flight one")

        await _start_running(tenant, child, target)
        counted = await DelegationRepository(tenant.session, tenant.organization_id).issued_by(
            source
        )
        assert counted == 1, (
            "the cap has stopped bounding concurrent fan-out, which is the runaway it "
            "exists to prevent"
        )

    async def test_a_stranded_run_stops_counting(self, tenant) -> None:
        """A `running` task whose execution is old is the stranded box the console draws."""
        ids = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        source, target = ids
        child = await self._child(tenant, target, "work that was interrupted")
        await self._record(tenant, source, target, child, "the interrupted one")
        await _start_running(tenant, child, target, age="now() - interval '3 hours'")
        counted = await DelegationRepository(tenant.session, tenant.organization_id).issued_by(
            source
        )
        assert counted == 0, (
            "a run interrupted three hours ago still holds the budget, which is the "
            "same leak as a never-started task"
        )


class TestParallelismIsNotBlockedByDuplicateDetection:
    """**One department may hold several pieces of work at once.**

    Reported as *"a task cannot be done when there is another task going — there should be
    multiple agents working at the same time and not blocking each other's work"*, and the
    mechanism was a unique index:

    ```
    UNIQUE (organization_id, owner_agent_id, intent_fingerprint)
    ```

    per agent, tenant-wide, keyed on **the first 16 tokens** of the goal. And the
    collision was not a near-duplicate at all — the two goals differed only by the run
    marker the platform appends, which sits after the window:

    ```
    Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm (lần chạy f648b6e3)
    Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm (lần chạy 21a4b662)
    ```

    Two separate runs, refused against each other, with *"an equivalent task is already
    active… move on to the next piece of work"*.

    **Duplicate detection is about a model re-asking, and re-asking happens inside one
    request.** So the index is scoped to the parent, and the fingerprint is the whole goal
    rather than a prefix of it.
    """

    GOAL = "Chọn nhà thầu cho gói thiết bị điều hòa của dự án Bãi Trầm"

    async def _delegate(
        self, tenant, source: str, target: str, goal: str, parent: str | None = None
    ):
        """One delegation, optionally under a named parent. Returns the child task."""

        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        repo = TaskRepository(tenant.session, tenant.organization_id)
        parent_id = parent or str(
            (
                await repo.create(
                    title="the goal",
                    goal="run the tender",
                    owner_agent_id=source,
                    allow_parallel=True,
                )
            ).id
        )
        child = await repo.create(
            title=goal[:60],
            goal=goal,
            owner_agent_id=target,
            parent_task_id=parent_id,
            allow_parallel=True,
            # **The fingerprint, as the executor writes it.** Without this the column is
            # NULL and the unique index is never consulted — so the "still refuses a
            # duplicate" test would have passed by proving nothing, which is the exact
            # mistake an older version of this file documents.
            intent_fingerprint=intent_fingerprint(
                organization_id=str(tenant.organization_id),
                task_type="analysis",
                goal=goal,
                owner_agent_id=target,
            ),
        )
        await delegations.record(
            parent_task_id=parent_id,
            child_task_id=child.id,
            source_agent_id=source,
            target_agent_id=target,
            objective=goal,
            platform_limits=DelegationLimits.platform_default(),
            path=DelegationPath(),
            parent_limits=DelegationLimits.platform_default(),
        )
        return child

    async def test_two_runs_of_the_same_tender_do_not_collide(self, tenant) -> None:
        """**The measured pair, verbatim.**

        The only difference is the run marker, and it used to land outside the 16-token
        window — so the second run of the same tender was refused as a duplicate of the
        first. This is the assertion that would have caught it.
        """
        source, target = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        first = await self._delegate(tenant, source, target, f"{self.GOAL} (lần chạy f648b6e3)")
        second = await self._delegate(tenant, source, target, f"{self.GOAL} (lần chạy 21a4b662)")
        assert str(first.id) != str(second.id)

    async def test_the_same_agent_holds_several_live_tasks(self, tenant) -> None:
        """The literal requirement: agents work at the same time, not one after another."""
        source, target = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        goals = [
            "Soạn JD cho kỹ sư chất lượng",
            "Xây dựng rubric đánh giá ứng viên",
            "Phân tích CV ứng viên ứng tuyển",
            "Lập danh mục vật tư cho dự án Bãi Trầm",
        ]
        for goal in goals:
            await self._delegate(tenant, source, target, goal)
        live = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM tasks WHERE organization_id = :o"
                    " AND owner_agent_id = :a AND status IN ('created','assigned','running')"
                ),
                {"o": str(tenant.organization_id), "a": target},
            )
        ).scalar()
        assert live == len(goals), (
            f"only {live} of {len(goals)} live tasks on one agent: a department can hold "
            "one piece of work at a time, which is a queue that admits one of anything"
        )

    async def test_the_same_request_still_refuses_a_duplicate(self, tenant) -> None:
        """**The other direction, and it is the case the index was built for.**

        Without this, "parallelism" could be achieved by deleting the control, and the
        first regression it caused was a fan-out cap that stopped meaning anything.
        """
        source, target = await _agent_ids(tenant.session, str(tenant.organization_id), 2)
        repo = TaskRepository(tenant.session, tenant.organization_id)
        parent = (
            await repo.create(
                title="g", goal="run the tender", owner_agent_id=source, allow_parallel=True
            )
        ).id
        # First ask: accepted.
        await self._delegate(tenant, source, target, self.GOAL, parent=parent)
        # **Same parent, same agent, same words: refused.** This is the re-ask the index
        # exists for, and it is what a fan-out cap stops meaning nothing without.
        with pytest.raises(ConflictError):
            await self._delegate(tenant, source, target, self.GOAL, parent=parent)
        live = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM tasks WHERE organization_id = :o"
                    " AND owner_agent_id = :a AND parent_task_id = :p"
                    " AND intent_fingerprint IS NOT NULL"
                ),
                {"o": str(tenant.organization_id), "a": target, "p": parent},
            )
        ).scalar()
        assert live == 1, (
            f"{live} tasks carry the same intent under one parent: a model re-asking is "
            "not parallel work, it is the duplicate the index exists to refuse"
        )
        assert ConflictError is not None
