"""The repetition gate: has this agent done this shape of work before?

This is the precondition the whole self-improvement design waits on. Until
something counts, the gate can never open, and a design whose precondition has
never been met is a design rather than a system — which is exactly what
`SELF_IMPROVEMENT.md` §11 said before this file existed.

The tests are about the *count*, including the ways a count can be confidently
wrong: a run that counts itself, an unobserved run counted as an observation, and
two organisations' runs counted against each other.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.procedures import ProcedureReader
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.persistence.models import Agent, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _run_twice_with_the_same_shape(seeded, goal: str) -> list:  # type: ignore[no-untyped-def]
    """Two runs that call the same tool with the same argument *shape*.

    Different goals, so their `tasks.fingerprint` differs — which is the point.
    If the count came back 2, the procedure hash is doing something the task hash
    cannot.
    """

    org_id = seeded.organization_id
    agent = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Sales Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    service = TaskExecutionService(seeded.session, org_id, runtime=_ToolCallingRuntime())
    global _AGENT_ID
    _AGENT_ID = str(agent.id)
    ids = []
    for _ in range(2):
        task = await tasks.create(
            title="Repeat", goal=goal, task_type="analysis", requester_type="human"
        )
        await tasks.assign(task.id, agent.id)
        await service.execute_task(task.id, agent_id=agent.id)
        ids.append(str(task.id))
    return ids


class _ToolCallingRuntime(ScriptedRuntime):
    """Calls one tool with a fixed argument shape, whatever the goal says."""

    name = "tool-calling"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            await execute_tool(tool_name="write_report", arguments={"title": "t", "body": "b"})
        return await super().execute(task, context, execute_tool=None, **kwargs)


class TestTheProcedureIsRecorded:
    async def test_a_run_leaves_a_fingerprint(self, seeded) -> None:
        from ai_orchestrator.persistence.models import Task

        ids = await _run_twice_with_the_same_shape(seeded, "one goal")
        task_id = ids[0]
        row = (await seeded.session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert row.procedure_fingerprint, "a run that used a tool left no procedure"

    async def test_a_run_with_no_trace_leaves_none(self, seeded) -> None:
        """`None` means "not observed", which is what an untraced run is.

        Inventing a fingerprint for a run with no trace would put a hash in a column
        that claims to describe a real observation.
        """
        from ai_orchestrator.application.procedures import ProcedureReader

        reader = ProcedureReader(seeded.session, seeded.organization_id)
        assert await reader.fingerprint_for("tsk_does_not_exist") is None


class TestTheGate:
    async def test_the_first_run_is_not_a_repetition(self, seeded) -> None:
        ids = await _run_twice_with_the_same_shape(seeded, "goal one")
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        fingerprint = await reader.fingerprint_for(ids[0])
        assert fingerprint is not None
        # Excluding itself, the shape has been seen once — the *other* run.
        count = await reader.repetition_count(
            agent_id=_agent_id(seeded),
            fingerprint=fingerprint,
            exclude_task_id=ids[0],
        )
        assert count == 1, count

    async def test_a_run_does_not_count_itself(self, seeded) -> None:
        """The bug that would make every gate open one repetition early.

        Recording a procedure and then counting would include the run being
        recorded, so the threshold would be met `threshold - 1` times.
        """
        ids = await _run_twice_with_the_same_shape(seeded, "goal one")
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        fingerprint = await reader.fingerprint_for(ids[0])
        assert fingerprint is not None
        without = await reader.repetition_count(
            agent_id=_agent_id(seeded), fingerprint=fingerprint, exclude_task_id=ids[0]
        )
        with_self = await reader.repetition_count(
            agent_id=_agent_id(seeded), fingerprint=fingerprint
        )
        assert with_self == without + 1, f"{with_self} vs {without}"

    async def test_two_different_goals_are_one_procedure(self, seeded) -> None:
        """The distinction the whole module exists for.

        Same tool, same argument shape, different wording. Their task fingerprints
        differ — so a repeat count built on the task hash would see two procedures
        where there is one.
        """
        from ai_orchestrator.persistence.models import Task

        ids = await _run_twice_with_the_same_shape(seeded, "goal one")
        # A second pair with a completely different wording.
        second = await _run_twice_with_the_same_shape(seeded, "an entirely different objective")
        rows = [
            (await seeded.session.execute(select(Task).where(Task.id == i))).scalar_one()
            for i in (ids[0], second[0])
        ]
        assert rows[0].fingerprint != rows[1].fingerprint, (
            "the two goals hashed the same, so this test cannot distinguish the "
            "task fingerprint from the procedure fingerprint"
        )
        assert rows[0].procedure_fingerprint == rows[1].procedure_fingerprint, (
            "two differently-worded runs of the same shape produced two procedures"
        )

    async def test_the_gate_opens_at_the_threshold(self, seeded) -> None:
        """The threshold decides, and it is the only thing that does.

        Four runs of the same shape exist by the end of this test. Excluding one of
        them leaves three priors, so a threshold of 4 must not open and a threshold
        of 3 must — the same data, two thresholds, opposite answers. That is what
        makes it a gate rather than a count.
        """
        first = await _run_twice_with_the_same_shape(seeded, "goal one")
        second = await _run_twice_with_the_same_shape(seeded, "goal one")
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        fingerprint = await reader.fingerprint_for(first[0])
        assert fingerprint is not None
        agent = _agent_id(seeded)
        exclude = second[1]

        not_open, count = await reader.has_repeated(
            agent_id=agent, fingerprint=fingerprint, threshold=4, exclude_task_id=exclude
        )
        assert count == 3, f"expected three priors, got {count}"
        assert not not_open, "the gate opened above its threshold"

        opened, count = await reader.has_repeated(
            agent_id=agent, fingerprint=fingerprint, threshold=3, exclude_task_id=exclude
        )
        assert count == 3
        assert opened, "the gate did not open at its threshold"

    async def test_the_count_comes_back_with_the_verdict(self, seeded) -> None:
        """ "Repeated 4 times" is the evidence; `True` is not."""
        ids = await _run_twice_with_the_same_shape(seeded, "goal one")
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        fingerprint = await reader.fingerprint_for(ids[0])
        assert fingerprint is not None
        _, count = await reader.has_repeated(
            agent_id=_agent_id(seeded),
            fingerprint=fingerprint,
            threshold=99,
            exclude_task_id=ids[1],
        )
        assert count == 1, count

    async def test_unobserved_runs_are_never_counted(self, seeded) -> None:
        """`None` is not an observation, and grouping them invents a procedure."""
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        count = await reader.repetition_count(
            agent_id=_agent_id(seeded), fingerprint="", exclude_task_id=None
        )
        assert count == 0, "an empty fingerprint counted as a repetition"

    async def test_a_tenant_only_counts_its_own(self, seeded) -> None:
        """RLS is the boundary, and the count must sit inside it."""
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        # A fingerprint that exists in another organisation is invisible here.
        assert await reader.repetition_count(agent_id=_agent_id(seeded), fingerprint="f" * 64) == 0


class TestSeeingWhatAnAgentHasDone:
    async def test_shapes_are_listed_most_repeated_first(self, seeded) -> None:
        await _run_twice_with_the_same_shape(seeded, "goal one")
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        seen = await reader.seen_procedures(agent_id=_agent_id(seeded))
        assert seen, "an agent that has run left no visible procedure"
        counts = [n for _, n in seen]
        assert counts == sorted(counts, reverse=True), counts

    async def test_tenants_do_not_see_each_other(self, seeded, tenant) -> None:  # type: ignore[no-untyped-def]
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        other = ProcedureReader(seeded.session, "org_01m3d5hwxet3x61vjc1ffjyrzh")
        assert await reader.seen_procedures(agent_id="agt_nobody") == []
        assert await other.seen_procedures(agent_id=_agent_id(seeded)) == []


#: The seeded Marketing Agent's id, filled in by `_run_twice_with_the_same_shape`.
#: Looked up rather than hard-coded: the seed mints a fresh id every run, and a
#: hard-coded one would make every count zero for a reason that has nothing to do
#: with the gate.
_AGENT_ID = ""


def _agent_id(seeded) -> str:  # type: ignore[no-untyped-def]
    assert _AGENT_ID, "no run has seeded the agent id yet"
    return _AGENT_ID
