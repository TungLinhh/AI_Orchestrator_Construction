"""A tool call must not flush the session it was handed.

F56. `TaskExecutionService._record_tool_call` carried a long comment explaining that
it must not flush, because a tool handler runs inside a flush of the parent task's
own transaction and a nested flush raises `InvalidRequestError: Session is already
flushing`. It then called `AuditService.record`, which flushes.

The whole test suite passed with the bug in place. 926 tests, and the trace this
method exists to write was never checked for arriving — so the suite agreed with a
comment rather than with the code.

It surfaced on the first live run: real runs of a `safe_web_search` task, the nested
flush, and then the failure handler could not record the failure either because the
transaction was already gone. The traceback ended on `Can't operate on closed
transaction`, which is the second-order error; the cause was twelve lines higher and
said nothing about audit rows.

**Why the counter is scoped to the tool call.** A task lifecycle flushes several times
legitimately — creating the row, moving the status, committing. Counting flushes
across a whole run therefore proves nothing: it reports six with or without the bug.
The property is narrower and is the one that matters: *while a tool handler is
running, the session must not flush.* So the counter is attached for exactly the
duration of the `execute_tool` call and detached immediately after.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.persistence.models import Agent, AuditLog, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

TOOL_INVOKE = "tool.invoke"


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


def _flush_counter(session: AsyncSession) -> tuple[list[int], object]:
    """A counter, and the callable that starts and stops it.

    Replaces `Session.flush` rather than listening for a flush event: the event also
    fires for cascade autoflush, and the bug is an explicit `await
    self._session.flush()` in a method documented as not having one. Those two are
    the same symptom with different causes, and only the explicit call is F56.
    """
    seen: list[int] = []
    original = session.flush

    async def counting_flush(*args: object, **kwargs: object) -> None:  # type: ignore[no-untyped-def]
        seen.append(1)
        await original(*args, **kwargs)  # type: ignore[arg-type]

    def arm() -> None:
        session.flush = counting_flush  # type: ignore[method-assign]

    def disarm() -> None:
        session.flush = original  # type: ignore[method-assign]

    return seen, (arm, disarm)


class _CountingRuntime(ScriptedRuntime):
    """Calls one tool, with the flush counter armed only for that call."""

    name = "counting"

    def __init__(self, arm: object, disarm: object) -> None:
        super().__init__()
        self._arm = arm
        self._disarm = disarm

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            self._arm()  # type: ignore[operator]
            try:
                await execute_tool(tool_name="write_report", arguments={"title": "t", "body": "b"})
            finally:
                self._disarm()  # type: ignore[operator]
        return await super().execute(task, context, execute_tool=None, **kwargs)


async def _run(seeded, runtime: ScriptedRuntime) -> str:  # type: ignore[no-untyped-def]
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Sales Agent",
            )
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    service = TaskExecutionService(seeded.session, seeded.organization_id, runtime=runtime)
    task = await tasks.create(
        title="A tool call", goal="write something", task_type="analysis", requester_type="human"
    )
    await tasks.assign(task.id, agent.id)
    await service.execute_task(task.id, agent_id=agent.id)
    return str(task.id)


class TestAToolCallDoesNotFlush:
    async def test_no_flush_happens_while_a_tool_handler_runs(self, seeded) -> None:
        seen, (arm, disarm) = _flush_counter(seeded.session)
        task_id = await _run(seeded, _CountingRuntime(arm, disarm))
        assert seen == [], (
            f"{len(seen)} flush(es) inside the tool call: a tool handler runs inside "
            "the parent task's flush, and a nested one raises "
            "InvalidRequestError: Session is already flushing"
        )
        assert task_id

    async def test_the_run_survives_a_tool_call(self, seeded) -> None:
        """The symptom, kept because it is the one an operator would actually see.

        With the bug, the run fails *and* the failure cannot be written, so the task
        is left in whatever state it was in when the transaction died. Asserting the
        task reached a terminal state catches that.

        Note what this does *not* assert: that the run **completed**. It did, when
        this test was written — but an earlier version of it accepted `failed` as
        well, on the reasoning that a terminal state is the thing that matters after a
        poisoned transaction. That reasoning is wrong here, and the live run proved
        it: three real runs of a free model hit its 32-call tool budget and were
        stopped by the platform, every one ending `failed`. A test that treats
        `failed` as success cannot tell a correct refusal from a defect, and the
        suite would have reported green over three consecutive failures.
        """
        _seen, (arm, disarm) = _flush_counter(seeded.session)
        task_id = await _run(seeded, _CountingRuntime(arm, disarm))
        await seeded.session.flush()
        from ai_orchestrator.persistence.models import Task

        task = (
            await seeded.session.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.organization_id == seeded.organization_id,
                )
            )
        ).scalar_one()
        assert str(task.status) == "completed", (
            f"the scripted run ended {task.status!r}: a scripted runtime makes one "
            "tool call and answers, so anything else is the platform, not the model"
        )


class TestTheTraceIsActuallyWritten:
    async def test_the_tool_trace_row_is_written(self, seeded) -> None:
        """The other half, and the reason the bug survived.

        `_record_tool_call` exists only to leave a durable row saying what the agent
        did. Its absence is silent: a run with no trace still completes, still reports
        success, and simply leaves nothing to review. A test counting procedures would
        read zero and conclude the platform had learned nothing.
        """
        _seen, (arm, disarm) = _flush_counter(seeded.session)
        task_id = await _run(seeded, _CountingRuntime(arm, disarm))
        await seeded.session.flush()
        rows = (
            (
                await seeded.session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.task_id == task_id,
                        AuditLog.action == TOOL_INVOKE,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows, (
            "the run completed but left no tool trace; the platform cannot see what its "
            "agents did, so no procedure can ever be observed repeating"
        )

    async def test_the_trace_row_names_the_tool(self, seeded) -> None:
        """Without the name, every procedure fingerprints to the same constant and the
        repetition gate can never distinguish two shapes of work."""
        _seen, (arm, disarm) = _flush_counter(seeded.session)
        task_id = await _run(seeded, _CountingRuntime(arm, disarm))
        await seeded.session.flush()
        rows = (
            (
                await seeded.session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.task_id == task_id,
                        AuditLog.action == TOOL_INVOKE,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [str(r.resource_id) for r in rows] == ["write_report"]

    async def test_the_trace_row_records_the_outcome(self, seeded) -> None:
        """`success`/`failure`, not `ok`.

        The vocabulary was misread once already while building the evidence reader —
        every call was marked as a refusal because the reader checked for a value the
        platform never writes. Pinning it here means the second reader inherits a
        tested fact rather than repeating the guess.
        """
        _seen, (arm, disarm) = _flush_counter(seeded.session)
        task_id = await _run(seeded, _CountingRuntime(arm, disarm))
        await seeded.session.flush()
        outcomes = {
            str(r.outcome)
            for r in (
                await seeded.session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.task_id == task_id,
                        AuditLog.action == TOOL_INVOKE,
                    )
                )
            ).scalars()
        }
        assert outcomes <= {"success", "failure", "blocked"}, (
            f"an audit outcome outside the known vocabulary appeared: {outcomes}"
        )
        assert outcomes == {"success"}
