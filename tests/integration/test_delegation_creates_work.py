"""Calling `delegate_to_agent` must create a delegation and a child task.

The claim under test is the one the whole platform rests on: a proposal is data,
the platform decides, and the decision leaves a row. A live run called
`delegate_to_agent` twenty-one times, every call reported success, and the
`delegations` table was empty. Whatever the cause, this test should have failed
long before that, and it did not exist.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Delegation, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _DelegatingRuntime(ScriptedRuntime):
    """Calls the delegation tool the way the bridge does, then finishes."""

    name = "delegating"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            result = await execute_tool(
                tool_name="delegate_to_agent",
                arguments={
                    "agent_name": "Finance Agent",
                    "objective": "approve the 5-laptop spend",
                },
            )
            self.last_result = result
        return await super().execute(task, context, execute_tool=None, **kwargs)


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _delegate_once(seeded):  # type: ignore[no-untyped-def]
    org_id = seeded.organization_id
    executive = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Executive Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    runtime = _DelegatingRuntime()
    service = TaskExecutionService(seeded.session, org_id, runtime=runtime)
    task = await tasks.create(
        title="Executive goal",
        goal="Buy 5 laptops for Marketing, needs approval before ordering",
        task_type="coordination",
        requester_type="human",
    )
    await tasks.assign(task.id, executive.id)
    await service.execute_task(task.id, agent_id=executive.id)
    return task, runtime


async def test_the_tool_reports_what_actually_happened(seeded) -> None:
    """The tool's own answer, before anything else is checked.

    Asserted first and on its own, because a handler that reports success while
    doing nothing is the failure this whole file exists for.
    """
    _, runtime = await _delegate_once(seeded)
    result = getattr(runtime, "last_result", None)
    assert result is not None, "the delegation tool was never called"
    assert result.ok, f"the tool reported failure: {result.error_kind} / {result.error_message}"
    assert result.output["accepted"] is True, result.output


async def test_a_delegation_row_exists(seeded) -> None:
    task, _ = await _delegate_once(seeded)
    rows = (
        (
            await seeded.session.execute(
                select(Delegation).where(Delegation.parent_task_id == str(task.id))
            )
        )
        .scalars()
        .all()
    )
    assert rows, (
        "the tool reported an accepted delegation and no delegation row exists; "
        "the platform told the model yes and wrote nothing down"
    )


async def test_a_child_task_exists_and_belongs_to_the_target(seeded) -> None:
    """A delegation with no work behind it is a row that lies."""
    task, _ = await _delegate_once(seeded)
    children = (
        (await seeded.session.execute(select(Task).where(Task.parent_task_id == str(task.id))))
        .scalars()
        .all()
    )
    assert children, "an accepted delegation created no child task"
    owner_ids = {c.owner_agent_id for c in children}
    assert len(owner_ids) == 1, f"children have more than one owner: {owner_ids}"


async def test_asking_twice_for_the_same_work_makes_one_child(seeded) -> None:
    """The model asked twice and got two children.

    It re-read its own tool result, decided the work was not done, and asked
    again. Two rows for one piece of work is how a fan-out cap stops meaning
    anything, and it is also a lie in the audit trail: the company appears to have
    assigned the same requisition twice.
    """
    org_id = seeded.organization_id
    executive = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Executive Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    runtime = _TwiceDelegatingRuntime()
    service = TaskExecutionService(seeded.session, org_id, runtime=runtime)
    task = await tasks.create(
        title="Executive goal",
        goal="Buy 5 laptops for Marketing, needs approval before ordering",
        task_type="coordination",
        requester_type="human",
    )
    await tasks.assign(task.id, executive.id)
    await service.execute_task(task.id, agent_id=executive.id)

    children = (
        (await seeded.session.execute(select(Task).where(Task.parent_task_id == str(task.id))))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"one piece of work produced {len(children)} children"


class _TwiceDelegatingRuntime(ScriptedRuntime):
    """Asks for the same delegation twice, in one run."""

    name = "twice-delegating"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            for _ in range(2):
                self.results = getattr(self, "results", [])
                self.results.append(
                    await execute_tool(
                        tool_name="delegate_to_agent",
                        arguments={
                            "agent_name": "Finance Agent",
                            "objective": "approve the 5-laptop spend",
                        },
                    )
                )
        return await super().execute(task, context, execute_tool=None, **kwargs)


class TestTheDuplicateGuardOutlivesTheProcess:
    """The existing duplicate test asked twice **inside one run**. The real defect was across runs.

    Measured in the development tenant: one parent task has **four** child tasks carrying the
    same title, the same owner agent, and the same status. The guard that should have stopped
    the second lived in an in-memory set on the `DelegationExecutor` instance, so it started
    every run empty.

    Driving that through the public entry point turned out to be impossible, and that is worth
    saying: `local_runner` refuses a second run of a finished task, and so does the state
    machine underneath it (`PreconditionError: illegal task transition: completed
    --complete-->`). The four rows therefore came from a parent that was re-driven while still
    non-terminal. Rather than contrive a state reset, these tests put the **pre-existing child
    in place directly** -- which is exactly the state a previous run leaves behind -- and then
    run the parent once and watch what the guard does.
    """

    @staticmethod
    async def _parent(seeded, *, title: str):  # type: ignore[no-untyped-def]
        org_id = seeded.organization_id
        executive = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Executive Agent"
                )
            )
        ).scalar_one()
        finance = (
            await seeded.session.execute(
                select(Agent).where(Agent.organization_id == org_id, Agent.name == "Finance Agent")
            )
        ).scalar_one()
        tasks = TaskRepository(seeded.session, org_id)
        parent = await tasks.create(
            title=title,
            goal="Buy 5 laptops for Marketing, needs approval before ordering",
            task_type="coordination",
            requester_type="human",
        )
        await tasks.assign(parent.id, executive.id)
        return parent, tasks, finance, executive

    @staticmethod
    async def _children(seeded, parent_id: str) -> list[object]:  # type: ignore[no-untyped-def]
        return list(
            (
                await seeded.session.execute(
                    select(Task)
                    .where(Task.parent_task_id == str(parent_id))
                    .order_by(Task.created_at)
                )
            )
            .scalars()
            .all()
        )

    async def test_an_open_child_from_an_earlier_pass_blocks_a_second_issue(self, seeded) -> None:
        """The guard the measurement says was missing.

        A live child with the same parent, agent and title stands in for a previous run. The
        run must leave that one child alone rather than adding a copy -- otherwise the queue
        shows the same work four times, which is what was measured.
        """
        parent, tasks, finance, executive = await self._parent(
            seeded, title="Executive goal, guarded"
        )
        # The objective `_DelegatingRuntime` proposes, truncated the way the executor does.
        await tasks.create(
            title="approve the 5-laptop spend",
            goal="approve the 5-laptop spend",
            task_type="coordination",
            parent_task_id=parent.id,
            owner_agent_id=finance.id,
            requester_type="agent",
            requester_agent_id=executive.id,
        )
        before = await self._children(seeded, parent.id)
        assert len(before) == 1, "the pre-existing child is the state under test"

        service = TaskExecutionService(
            seeded.session, seeded.organization_id, runtime=_DelegatingRuntime()
        )
        await service.execute_task(parent.id)
        after = await self._children(seeded, parent.id)

        assert len(after) == 1, (
            f"the guard let a duplicate through: {len(after)} children, {[c.title for c in after]}"
        )
        assert after[0].id == before[0].id, "and it is the child that was already there"

    async def test_a_real_run_leaves_a_child_the_guard_can_see(self, seeded) -> None:
        """The guard has to recognise what a *real* run leaves behind, not a hand-built row.

        The other two tests here place the pre-existing child with `TaskRepository.create`
        directly. That is a shortcut with a real limitation: it writes no `delegations` row, so
        `coordination_may_complete` (which counts delegation *rows*, not child tasks) sees zero
        and fails the parent with `no_delegation`. The shortcut therefore cannot answer
        "does the guard fire in production?", and this test can: one ordinary run, then ask the
        guard's own query what it finds.

        Asserted behaviourally, in the terms the business uses — the same parent, the same
        agent, the same work — and then the same query again once the work is finished, which
        is the other half of the rule.
        """
        org_id = seeded.organization_id
        executive = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Executive Agent"
                )
            )
        ).scalar_one()
        finance = (
            await seeded.session.execute(
                select(Agent).where(Agent.organization_id == org_id, Agent.name == "Finance Agent")
            )
        ).scalar_one()
        tasks = TaskRepository(seeded.session, org_id)
        parent = await tasks.create(
            title="Executive goal, real run",
            goal="Buy 5 laptops for Marketing, needs approval before ordering",
            task_type="coordination",
            requester_type="human",
        )
        await tasks.assign(parent.id, executive.id)
        service = TaskExecutionService(seeded.session, org_id, runtime=_DelegatingRuntime())
        await service.execute_task(parent.id)

        children = await self._children(seeded, parent.id)
        assert len(children) == 1, f"the run made {len(children)} children"

        # The guard's own question, answered against the row a real run wrote.
        found = await tasks.find_live_child_of(
            parent_task_id=parent.id,
            owner_agent_id=str(finance.id),
            title=children[0].title,
        )
        assert found is not None and found.id == children[0].id, (
            "the durable guard cannot see a child a real run created; it would let the next "
            "run duplicate the work"
        )

        # And once the work is finished, the guard must stop seeing it.
        await tasks.transition(children[0].id, Transition.BEGIN_WORK)
        await tasks.transition(children[0].id, Transition.COMPLETE)
        gone = await tasks.find_live_child_of(
            parent_task_id=parent.id,
            owner_agent_id=str(finance.id),
            title=children[0].title,
        )
        assert gone is None, "a finished child must not keep blocking the same work"

    async def test_a_finished_child_does_not_block_the_work_being_asked_again(self, seeded) -> None:
        """A guard that is too strict is a second bug in the first one's clothes.

        Once the delegated work is **finished**, asking for it again is legitimate -- a
        follow-up, a new phase, a re-check. The rule counts *live* children only, and this is
        the assertion that stops it quietly becoming "never delegate this title twice for this
        parent", which would be its own kind of wrong.
        """
        parent, tasks, finance, executive = await self._parent(
            seeded, title="Executive goal, re-asked"
        )
        child = await tasks.create(
            title="approve the 5-laptop spend",
            goal="approve the 5-laptop spend",
            task_type="coordination",
            parent_task_id=parent.id,
            owner_agent_id=finance.id,
            requester_type="agent",
            requester_agent_id=executive.id,
        )
        await tasks.transition(child.id, Transition.BEGIN_WORK)
        await tasks.transition(child.id, Transition.COMPLETE)

        service = TaskExecutionService(
            seeded.session, seeded.organization_id, runtime=_DelegatingRuntime()
        )
        await service.execute_task(parent.id)
        after = await self._children(seeded, parent.id)

        assert len(after) == 2, (
            "a finished child must not block the work being asked for again; "
            f"got {len(after)} children"
        )
        assert after[0].id == child.id, "and the finished one is still there"

    async def test_a_reworded_objective_still_counts_as_the_same_work(self, seeded) -> None:
        """The measured failure, exactly: **wording is not what makes work the same.**

        The four duplicate children in the development tenant carry one title and **four
        different fingerprints**, because `TaskRepository.create` derives its dedup key from
        the objective's wording. Rephrasing the same instruction is enough to walk past the
        repository's check -- and past the executor's in-memory set too, because that set only
        ever covered one run.

        The guard added here compares the **title the child would carry**, which is the
        executor's own truncation of the proposed objective, so a reworded proposal that lands
        on the same first 120 characters is refused. Asserted directly: the pre-existing child
        has a *different* fingerprint from the one the run would produce, so the repository's
        check cannot be what stops it.
        """
        parent, tasks, finance, executive = await self._parent(
            seeded, title="Executive goal, reworded"
        )
        existing = await tasks.create(
            title="approve the 5-laptop spend",
            goal="approve the 5-laptop spend",
            task_type="coordination",
            parent_task_id=parent.id,
            owner_agent_id=finance.id,
            requester_type="agent",
            requester_agent_id=executive.id,
        )
        # A *different* fingerprint for the same work: this is the reworded variant.
        reworded = await tasks.create(
            title="approve the 5-laptop spend",
            goal="approve the 5-laptop spend, this time with the receipt attached",
            task_type="coordination",
            parent_task_id=parent.id,
            owner_agent_id=finance.id,
            requester_type="agent",
            requester_agent_id=executive.id,
            allow_parallel=True,
        )
        assert existing.fingerprint != reworded.fingerprint, (
            "the premise: two spellings of the same instruction must not share a fingerprint, "
            "or the repository's own check would have caught this"
        )
        del reworded

        service = TaskExecutionService(
            seeded.session, seeded.organization_id, runtime=_DelegatingRuntime()
        )
        await service.execute_task(parent.id)
        after = await self._children(seeded, parent.id)

        assert len(after) == 2, (
            "the two reworded children that already existed must both survive, and the run "
            f"must not add a third; got {len(after)}"
        )


async def test_the_parent_is_not_completed_while_children_are_open(seeded) -> None:
    """The goal cannot close with the work still open."""
    task, _ = await _delegate_once(seeded)
    fresh = (await seeded.session.execute(select(Task).where(Task.id == str(task.id)))).scalar_one()
    children = (
        (await seeded.session.execute(select(Task).where(Task.parent_task_id == str(task.id))))
        .scalars()
        .all()
    )
    if children and all(c.status in {"created", "running"} for c in children):
        assert fresh.status != "completed", "the parent reported completed with unfinished children"


class TestTheChiefIsNotHandedTheWork:
    """A root coordinator must not be given the material its own answer needs.

    **This is the change that makes the organisation autonomous**, and it was found by
    running the first real procurement goal of the day. Measured:

        supplier-tender, real free model:
          in=54128 out=9223 tools=15 models=16
          summary: "**Nhà thầu đề xuất trúng thầu: Công ty Toàn Cầu (Báo giá C)**
                    ... lý do theo tiêu chí ..."
          task.failed  category=no_delegation

        offer-approval:  task.failed  category=budget_error
          reason='the agent exceeded its turn budget and was stopped'

    Two categories, one cause: the chief was handed the three bids and the three
    salaries, and it answered. `no_delegation` caught the first — correctly, because a
    fleet that answers everything itself is not a fleet — and the second was caught
    only because answering ran past its own turn budget.
    """

    async def test_a_root_coordinator_does_not_see_the_brief(self, seeded: Any) -> None:
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=ScriptedRuntime(),
        )
        repo = TaskRepository(seeded.session, seeded.organization_id)
        chief = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Executive Agent",
                )
            )
        ).scalar_one()
        root = await repo.create(
            title="Choose a supplier",
            goal="Recommend the winning bid for the HVAC package.",
            task_type="coordination",
            owner_agent_id=chief.id,
            input={
                "owning_office": "front-office",
                "owning_department": "procurement",
                "brief": "Bid A 1.24bn, Bid B 1.18bn, Bid C 1.09bn",
            },
        )
        seen = service._input_the_agent_sees(root, None)
        assert "brief" not in seen, (
            "the chief can read the bids, so it answers instead of delegating — which "
            "is a failed task containing a perfectly good answer"
        )
        assert seen.get("owning_department") == "procurement", (
            "the routing keys must survive: the chief still has to know who owns this"
        )
        assert root.input.get("brief"), (
            "the brief must stay on the row, or the department has nothing either"
        )

    async def test_a_department_still_sees_the_brief(self, seeded: Any) -> None:
        """Otherwise the fix would have moved the problem rather than solving it."""
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=ScriptedRuntime(),
        )
        repo = TaskRepository(seeded.session, seeded.organization_id)
        chief = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Executive Agent",
                )
            )
        ).scalar_one()
        root = await repo.create(
            title="Choose a supplier",
            goal="Recommend the winning bid.",
            task_type="coordination",
            owner_agent_id=chief.id,
            input={"brief": "Bid A 1.24bn"},
        )
        child = await repo.create(
            title="Compare the bids",
            goal="Compare the three bids.",
            task_type="execution",
            parent_task_id=root.id,
            input={"brief": "Bid A 1.24bn"},
        )
        assert service._input_the_agent_sees(child, None).get("brief"), (
            "a department with no material cannot answer, and the brief must arrive with the work"
        )
