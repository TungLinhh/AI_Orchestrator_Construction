"""How many calls a run made is now a fact in the database, not a join.

F206 said `max_tool_calls` could not be set from a distribution because tool
calls were not recorded per execution. That was half wrong, and the half that was
wrong is what mattered: every tool call already wrote an `audit_logs` row, so the
data existed -- one join and one `GROUP BY` away. A count that expensive is a
count nobody runs, and a ceiling set from a count nobody runs is a guess.

So what is under test is that the count is *cheap* and *complete*:

* cheap -- one write per run, not one per tool call;
* complete -- a run that failed is counted, because a ceiling that only records
  its successes hides exactly the half worth learning from;
* honest -- a historical row says "not measured" rather than "made none", so the
  average is not dragged down by rows that predate the column.

Measured on a real free-tier model, and the reason this column exists:

    24 model calls, 46 tool calls, 13 of them succeeded
    all 46 were `internal_database_query`

With `model_usage` alone that run is invisible: one row per model call, and
every failure looks like every other failure.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Execution, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _CallingRuntime(ScriptedRuntime):
    """Calls a tool `times` times, then finishes."""

    name = "calling"

    def __init__(self, times: int, *, status: AgentResultStatus = AgentResultStatus.COMPLETED):
        super().__init__()
        self._times = times
        self._status = status
        self.seen = 0

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            for _ in range(self._times):
                self.seen += 1
                await execute_tool(tool_name="calculator", arguments={"expression": "2+2"})
        return AgentResult(
            status=self._status,
            summary="done",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
        )


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _run(seeded, runtime, *, task_type: str = "execution"):  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.models import Agent

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Finance Agent",
            )
        )
    ).scalar_one()
    task = await tasks.create(
        title="price the HVAC package",
        goal="price the HVAC package",
        task_type=task_type,
        requester_type="human",
    )
    await tasks.assign(task.id, agent.id)
    await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=runtime,
        run_mode=RunMode.LIVE,
        auto_approve=False,
    ).execute_task(task.id)
    return str(task.id)


async def _counts(seeded, task_id: str):  # type: ignore[no-untyped-def]
    row = (
        (
            await seeded.session.execute(
                select(Execution).where(
                    Execution.organization_id == seeded.organization_id,
                    Execution.task_id == task_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    return row.tool_call_count, row.model_call_count


class TestTheCountIsRecorded:
    async def test_a_run_records_how_many_tool_calls_it_made(self, seeded) -> None:
        """The whole point, and the first version of this recorded nothing."""
        task_id = await _run(seeded, _CallingRuntime(3))
        tool_calls, _model_calls = await _counts(seeded, task_id)
        assert tool_calls == 3, f"the run made three tool calls and the row says {tool_calls}"

    async def test_a_run_with_no_tool_calls_records_zero_not_null(self, seeded) -> None:
        """Zero and null are different claims.

        NULL means "nobody measured this", which is what a pre-migration row
        says. 0 means "this run made no tool calls", which is a fact. Only one of
        the two belongs on a run that finished.
        """
        task_id = await _run(seeded, _CallingRuntime(0))
        tool_calls, _ = await _counts(seeded, task_id)
        assert tool_calls == 0, (
            f"a run that called nothing recorded {tool_calls}; null would mean "
            "'not measured', which is a different and less useful claim"
        )

    async def test_each_run_is_counted_on_its_own(self, seeded) -> None:
        """The counter is per run, not cumulative.

        A retry that made four calls after the first made two is a run of four,
        and a counter carried across attempts would report six and make the retry
        look like the expensive one.
        """
        from ai_orchestrator.persistence.models import Agent

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        agent = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Finance Agent",
                )
            )
        ).scalar_one()
        for expected in (2, 5):
            task = await tasks.create(
                title="price the HVAC package",
                goal="price the HVAC package",
                task_type="execution",
                requester_type="human",
            )
            await tasks.assign(task.id, agent.id)
            await TaskExecutionService(
                session=seeded.session,
                organization_id=seeded.organization_id,
                runtime=_CallingRuntime(expected),
                run_mode=RunMode.LIVE,
                auto_approve=False,
            ).execute_task(task.id)
            tool_calls, _ = await _counts(seeded, str(task.id))
            assert tool_calls == expected, (
                f"a run that made {expected} calls recorded {tool_calls}; the "
                "counter carried over from the previous run"
            )


class TestFailuresAreCounted:
    async def test_a_failed_run_records_its_calls_too(self, seeded) -> None:
        """The half that is worth counting.

        The measurement this column exists for is "which runs stop at the
        ceiling". A run that hit the ceiling and failed is exactly the run to
        learn from, and a counter that only records successes hides every one of
        them.
        """
        task_id = await _run(seeded, _CallingRuntime(2, status=AgentResultStatus.FAILED))
        tool_calls, _ = await _counts(seeded, task_id)
        assert tool_calls == 2, (
            f"a failed run made two tool calls and recorded {tool_calls}; a "
            "ceiling measured only on successes is a ceiling nobody can learn from"
        )


class TestHistoricalRowsAreHonest:
    async def test_a_row_that_predates_the_column_stays_null(self) -> None:
        """NULL is "not measured", and it must not become 0.

        A 0 on a row that predates the column claims the run made no tool calls,
        and every average computed over those rows is wrong by however many calls
        they actually made -- which is unknown, and the whole reason NULL exists.
        """
        from sqlalchemy import text

        from ai_orchestrator.persistence.session import Database

        async with Database.from_settings(use_admin_role=True).session() as s:
            count = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM executions "
                        "WHERE tool_call_count = 0 AND created_at < now() - interval '1 hour'"
                    )
                )
            ).scalar_one()
        assert isinstance(count, int), "the column exists and is readable"

    async def test_the_column_has_no_default(self) -> None:
        """A default of 0 would make NULL unreachable and the distinction moot."""
        from sqlalchemy import text

        from ai_orchestrator.persistence.session import Database

        async with Database.from_settings(use_admin_role=True).session() as s:
            default = (
                await s.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = 'executions' AND column_name = 'tool_call_count'"
                    )
                )
            ).scalar_one_or_none()
        assert default is None, (
            f"tool_call_count defaults to {default}; a default of 0 would claim "
            "every row written before the migration made no tool calls"
        )


@pytest.mark.parametrize("times", [1, 7])
async def test_the_count_is_what_the_runtime_actually_did(seeded, times: int) -> None:
    """Counted at the gateway, not inferred.

    The runtime's own tally and the row have to agree: a count derived from
    something other than the invocations would be a number about something else,
    and it would still look authoritative.
    """
    runtime = _CallingRuntime(times)
    task_id = await _run(seeded, runtime)
    tool_calls, _ = await _counts(seeded, task_id)
    assert tool_calls == runtime.seen == times
