"""A failure must be reported with its own cause, not a later one's.

A live run died with:

```
sqlalchemy.exc.InvalidRequestError: Can't operate on closed transaction inside
context manager. The transaction was rolled back due to an exception
```

and that was **not** the bug. It was the *failure handler* failing: the work had
already raised, the transaction had already rolled back, and `_fail` then tried to
write the `failed` status through the same dead session. The write raised, and the
raise replaced the real exception in the traceback.

So an operator reading the traceback investigates a session-lifecycle bug that does
not exist, while the actual cause — whatever the model or a tool did — is only in
the first exception's message, twenty lines up, with no `During handling of the
above exception` marker to connect them.

The property: when the session is gone, the cause in hand is what gets reported.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.enums import TaskStatus
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.persistence.models import Task as TaskRow
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


class _PoisoningRuntime:
    """Raises, and leaves the session unusable.

    The shape of a real mid-flush failure: an exception inside a flush rolls the
    transaction back, so by the time the failure handler runs there is no
    transaction left to write through.
    """

    name = "poisoning"
    _session: object = None

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        # Roll the transaction back, exactly as an exception inside a flush does.
        await self._session.rollback()  # type: ignore[attr-defined]
        raise RuntimeError("the model returned a shape nothing could parse")


class _CleanRuntime:
    name = "clean"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("the model returned a shape nothing could parse")


async def _service(seeded, runtime):  # type: ignore[no-untyped-def]

    service = TaskExecutionService(seeded.session, seeded.organization_id, runtime=runtime)
    if hasattr(runtime, "_session"):
        runtime._session = seeded.session
    return service


async def _one_task(seeded, service):  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.models import Agent
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    org_id = seeded.organization_id
    agent = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Executive Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    task = await tasks.create(
        title="Will fail", goal="something", task_type="analysis", requester_type="human"
    )
    await tasks.assign(task.id, agent.id)
    return task, agent


class TestAFailureIsReportedWithItsOwnCause:
    async def test_a_healthy_session_records_the_failure(self, seeded) -> None:
        """The ordinary path, asserted so the guarded one means something.

        With a usable session the `failed` status is written to the database, which
        is the whole point of the failure path.
        """
        service = await _service(seeded, _CleanRuntime())
        task, agent = await _one_task(seeded, service)
        outcome = await service.execute_task(task.id, agent_id=agent.id)

        assert outcome.status is TaskStatus.FAILED
        assert "the model returned a shape nothing could parse" in outcome.summary
        fresh = (
            await seeded.session.execute(select(TaskRow).where(TaskRow.id == str(task.id)))
        ).scalar_one()
        assert fresh.status == "failed", "a usable session failed to record the failure"

    async def test_a_dead_session_does_not_replace_the_cause(self, seeded) -> None:
        """The defect. The reported summary must be the *original* exception."""
        service = await _service(seeded, _PoisoningRuntime())
        task, agent = await _one_task(seeded, service)
        outcome = await service.execute_task(task.id, agent_id=agent.id)

        assert outcome.status is TaskStatus.FAILED
        assert "the model returned a shape nothing could parse" in outcome.summary, (
            f"the cause was replaced by a later failure: {outcome.summary[:300]}"
        )
        assert "closed transaction" not in outcome.summary, (
            "the session error is being reported as the cause"
        )

    async def test_no_third_error_escapes(self, seeded) -> None:
        """The failure handler must not raise on its way out.

        Before the fix this propagated `InvalidRequestError` out of
        `execute_task`, so the caller saw a crash rather than a failed task.
        """
        service = await _service(seeded, _PoisoningRuntime())
        task, agent = await _one_task(seeded, service)
        try:
            await service.execute_task(task.id, agent_id=agent.id)
        except InvalidRequestError as exc:  # pragma: no cover - the regression
            pytest.fail(f"execute_task raised {type(exc).__name__}: {exc}")
