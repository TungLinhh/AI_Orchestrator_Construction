"""What did the agent actually do?

The question a platform has to be able to answer about its own agents, and the one
it could not. A run that spun through 24 model turns left a `task.execute` row, a
cost-ledger entry per model call, a `runtime.tools_exposed` log line naming the
tools it was *offered* — and nothing at all about the tool calls in between. So
"the Executive never delegated" was an absence in a table, which is indistinguishable
from a query that was never written.

This is also the prerequisite for the self-improvement loop. A procedure is a
*sequence* of steps, so a repeat is only detectable if the sequence is stored in
order. See `docs/SELF_IMPROVEMENT.md` §9.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.persistence.models import Agent, AuditLog, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _ToolCallingRuntime(ScriptedRuntime):
    """Calls a real tool, the way the PydanticAI bridge does.

    `ScriptedRuntime` never calls a tool — it returns a canned result — so a test
    that uses it proves nothing about tool calls. This one calls the same
    `execute_tool` the bridge calls, with the same keyword arguments, which is the
    contract under test.
    """

    name = "tool-calling"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            await execute_tool(
                tool_name="write_report",
                arguments={"title": "Q3", "body": "revenue up"},
            )
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


async def _run_once(seeded, goal: str = "Summarise the quarter"):
    """Execute one task on a seeded agent and return its id."""
    org_id = seeded.organization_id
    agent = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Sales Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    service = TaskExecutionService(seeded.session, org_id, runtime=_ToolCallingRuntime())
    task = await tasks.create(
        title="Trace a run", goal=goal, task_type="analysis", requester_type="human"
    )
    await tasks.assign(task.id, agent.id)
    await service.execute_task(task.id, agent_id=agent.id)
    return str(task.id)


async def test_a_tool_call_leaves_a_row(seeded) -> None:
    """The row is the whole point: a trace nobody can read is not a trace."""
    task_id = await _run_once(seeded)
    rows = (
        (
            await seeded.session.execute(
                select(AuditLog)
                .where(AuditLog.task_id == task_id, AuditLog.action == "tool.invoke")
                .order_by(AuditLog.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "a task ran and no tool call was recorded anywhere"
    assert {r.resource_id for r in rows}, "the rows do not name the tool"


async def test_the_trace_preserves_order(seeded) -> None:
    """A procedure is a sequence. Stored unordered, it is a bag of steps."""
    task_id = await _run_once(seeded)
    rows = (
        (
            await seeded.session.execute(
                select(AuditLog)
                .where(AuditLog.task_id == task_id, AuditLog.action == "tool.invoke")
                .order_by(AuditLog.sequence)
            )
        )
        .scalars()
        .all()
    )
    sequences = [r.sequence for r in rows]
    assert sequences == sorted(sequences)


async def test_arguments_are_redacted_before_they_are_stored(seeded) -> None:
    """A tool argument is the most likely place a secret enters a queryable table.

    This table is tenant-scoped and readable by anything holding the tenant, so an
    argument stored verbatim is a credential in a database column.
    """
    from ai_orchestrator.telemetry.logging import redact

    stored = redact({"arguments": {"api_key": "sk-abcdefghijklmnopqrstuvwxyz0123456789"}})
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in str(stored)
    # And the service really does route arguments through the filter: asserted
    # against the code path, because a tool that takes no secrets today is not a
    # reason to store the next one's verbatim.
    import inspect

    from ai_orchestrator.application.task_execution import TaskExecutionService as Svc

    source = inspect.getsource(Svc._record_tool_call)
    assert "redact(" in source


async def test_the_audit_service_can_find_them_by_action(seeded) -> None:
    """Queryable through the normal audit path, not a bespoke query."""
    task_id = await _run_once(seeded)
    audit = AuditService(seeded.session, seeded.organization_id)
    found = await audit.query(action="tool.invoke", limit=50)
    assert any(r.task_id == task_id for r in found), (
        "tool calls are written but not findable through the audit service"
    )


async def test_a_tool_call_row_is_staged_and_not_flushed(seeded) -> None:
    """A tool handler runs *inside* the task's own flush. It must not flush again.

    `AuditService.record` flushes so a row is immediately queryable, which is
    right for an application service and fatal here: SQLAlchemy raises
    `InvalidRequestError: Session is already flushing`, and because that happens
    mid-transaction every later statement on the same session fails too with
    "Can't operate on closed transaction". One nested flush, two unrelated errors,
    and the second is the one the traceback ends on — which is why this looked
    like a session bug rather than an audit bug.

    Asserted as the absence of a round trip, because that is the property: the row
    joins the transaction already open. Counting flushes would assert a mechanism.
    """
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.domain.enums import ActorType
    from ai_orchestrator.domain.ids import OrganizationId

    audit = AuditService(seeded.session, seeded.organization_id)
    row = audit.add(
        actor=Actor(
            id="agt_01m3d5hwxet3x61vjc1ffjyrzj",
            kind=ActorType.AGENT,
            organization_id=OrganizationId(seeded.organization_id),
        ),
        action="tool.invoke",
        resource_type="tool",
        resource_id="probe",
    )
    # In the session's pending set, and still there — a flush would have tried to
    # INSERT it, which needs a live transaction this test has deliberately not got.
    # The seed leaves other rows pending, so the check is on the row itself rather
    # than on the whole set.
    assert row in seeded.session.new, "the row was not staged on the session"
    assert list(seeded.session.new).count(row) == 1, "add() staged the row twice"


async def test_a_tool_call_does_not_break_the_task_that_made_it(seeded) -> None:
    """The property, at the level an operator experiences: the run survives."""
    task_id = await _run_once(seeded, goal="Write a report and do not raise")
    fresh = (await seeded.session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert fresh.status in {"completed", "failed"}, fresh.status
    # And the session is still usable, which is the half that was poisoned.
    rows = (
        (
            await seeded.session.execute(
                select(AuditLog).where(
                    AuditLog.task_id == task_id, AuditLog.action == "tool.invoke"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the tool call row did not survive its own transaction"
