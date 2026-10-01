"""Work goes down; the answer has to come back up.

A manager who delegates six things and gets six green ticks has a queue, not an
answer. The sentence that makes a decision possible -- "the tender came in 12%
over budget, I recommend splitting it into two lots" -- exists in exactly one
place, in the child agent's own summary, and if nobody lifts it the parent can
only ever report that it is still waiting.

These tests assert the report travels, that it is attributed to the agent that
wrote it, and that it is marked as another agent's words rather than the parent's
own. The last one is the security property: a parent that treats a child's
report as an instruction is a parent that anybody below it can steer, which
inverts the whole point of the hierarchy.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Agent, Execution, Organization
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


async def _agent_id(seeded) -> str:  # type: ignore[no-untyped-def]
    """Any active agent, so a task has an owner before it can begin work."""
    return (
        await seeded.session.execute(
            select(Agent.id).where(Agent.organization_id == seeded.organization_id).limit(1)
        )
    ).scalar_one()


async def _say(seeded, task_id: str, summary: str) -> None:  # type: ignore[no-untyped-def]
    """Record what an agent said, the way the platform does.

    On `executions`, not on `tasks`: the `tasks` table has no summary column,
    which is worth stating here because looking for one is the natural first
    mistake and the error it produces is a column that does not exist.
    """
    execution = (
        await seeded.session.execute(
            select(Execution)
            .where(
                Execution.organization_id == seeded.organization_id,
                Execution.task_id == task_id,
            )
            .order_by(Execution.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert execution is not None, (
        "no execution row for a completed task: the platform records what an "
        "agent said when it runs the task, and a task that never ran has said "
        "nothing. A skip here would hide exactly the bug this test exists for."
    )
    execution.summary = summary
    await seeded.session.flush()


def _service(seeded):  # type: ignore[no-untyped-def]
    from ai_orchestrator.agent_runtime import ScriptedRuntime

    return TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=ScriptedRuntime(),
        run_mode=RunMode.SIMULATION,
    )


class _SayingRuntime(ScriptedRuntime):
    """Finishes a task with a fixed sentence, the way a real run reports."""

    name = "saying"

    def __init__(self, said: str) -> None:
        super().__init__()
        self._said = said

    async def execute(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=self._said,
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
        )


async def _run_child(seeded, tasks, child_id: str, said: str):  # type: ignore[no-untyped-def]
    """Run a child task for real, so it has an execution and a summary.

    Driving the state machine by hand would be a shortcut that hides the
    premise: the summary an agent reports is written by the run, not by the
    transition, and a test that sets it by hand is testing a fixture.
    """

    service = TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=_SayingRuntime(said),
        run_mode=RunMode.SIMULATION,
    )
    await service.execute_task(child_id)
    await seeded.session.flush()
    return service


class TestReportsTravelBackUp:
    async def test_a_finished_childs_summary_reaches_the_parent(self, seeded) -> None:
        """The parent's next context contains the child's own words.

        Asserted on the string the child wrote, not on a format: what has to
        survive is the report, and how it is labelled is a presentation choice.
        """
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks.create(
            title="award the retrofit contract",
            goal="award the retrofit contract",
            task_type="coordination",
            requester_type="human",
        )
        child = await tasks.create(
            title="price the HVAC package",
            goal="price the HVAC package",
            task_type="execution",
            parent_task_id=parent.id,
            requester_type="human",
        )
        await tasks.assign(child.id, await _agent_id(seeded))
        await _run_child(seeded, tasks, child.id, "12% over budget; split into two lots")

        service = _service(seeded)
        reports = await service._reports_from_children(parent)
        assert reports, (
            "the child finished and said something; the parent must be able to "
            "read it, otherwise delegation collects work and returns nothing"
        )
        assert any("12% over budget" in r for r in reports), (
            f"the child's own sentence did not survive: {reports}"
        )

    async def test_work_still_in_flight_is_not_reported_as_done(self, seeded) -> None:
        """An unfinished child has no report, and must not be given one.

        A parent that receives a report from a task which has not finished is
        being told the work is done, and the failure this prevents is a manager
        closing a goal with a hole in it.
        """
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks.create(
            title="award the retrofit contract",
            goal="award the retrofit contract",
            task_type="coordination",
            requester_type="human",
        )
        await tasks.create(
            title="price the HVAC package",
            goal="price the HVAC package",
            task_type="execution",
            parent_task_id=parent.id,
            requester_type="human",
        )

        service = _service(seeded)
        reports = await service._reports_from_children(parent)
        assert not reports, f"an unfinished child was reported as finished: {reports}"

    async def test_a_child_without_a_summary_is_not_reported(self, seeded) -> None:
        """Completed with nothing to say is not the same as completed with a report.

        Emitting an empty report would put a blank line in the parent's context
        and read as "this agent had nothing to add", which is a claim about
        another agent's judgement that nobody made.
        """
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks.create(
            title="award the retrofit contract",
            goal="award the retrofit contract",
            task_type="coordination",
            requester_type="human",
        )
        child = await tasks.create(
            title="price the HVAC package",
            goal="price the HVAC package",
            task_type="execution",
            parent_task_id=parent.id,
            requester_type="human",
        )
        await tasks.assign(child.id, await _agent_id(seeded))
        await _run_child(seeded, tasks, child.id, "")

        service = _service(seeded)
        reports = await service._reports_from_children(parent)
        assert not reports, f"a child with nothing to say was given a report: {reports}"

    async def test_another_parent_does_not_see_these_reports(self, seeded) -> None:
        """Reports are not a broadcast.

        Two goals running at once must not see each other's children's work: a
        parent shown a stranger's report will either act on it or distrust all of
        them, and both outcomes are worse than showing nothing.
        """
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        mine = await tasks.create(
            title="my goal", goal="my goal", task_type="coordination", requester_type="human"
        )
        theirs = await tasks.create(
            title="their goal", goal="their goal", task_type="coordination", requester_type="human"
        )
        child = await tasks.create(
            title="price the HVAC package",
            goal="price the HVAC package",
            task_type="execution",
            parent_task_id=theirs.id,
            requester_type="human",
        )
        await tasks.assign(child.id, await _agent_id(seeded))
        await tasks.assign(child.id, await _agent_id(seeded))
        await _run_child(seeded, tasks, child.id, "a finding that belongs to them")

        service = _service(seeded)
        assert not await service._reports_from_children(mine)
        assert await service._reports_from_children(theirs)

    async def test_the_report_is_attributed_to_its_author(self, seeded) -> None:
        """A manager has to know who said it.

        "12% over budget" from the estimator and the same sentence from the QS are
        different facts, and a report that arrives without its author is an
        assertion rather than a finding.
        """
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks.create(
            title="award the retrofit contract",
            goal="award the retrofit contract",
            task_type="coordination",
            requester_type="human",
        )
        child = await tasks.create(
            title="price the HVAC package",
            goal="price the HVAC package",
            task_type="execution",
            parent_task_id=parent.id,
            requester_type="human",
        )
        await tasks.assign(child.id, await _agent_id(seeded))
        await _run_child(seeded, tasks, child.id, "12% over budget")

        service = _service(seeded)
        reports = await service._reports_from_children(parent)
        assert any("price the HVAC package" in r for r in reports), (
            f"a report with no author is an assertion: {reports}"
        )
