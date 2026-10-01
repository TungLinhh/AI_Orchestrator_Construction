"""An agent asking for approval must actually reach a person.

The task status `waiting_for_approval` is not a request. It is a state that says
somebody is deciding, and with nothing behind it, nobody has been asked: the task
waits forever, the inbox is empty, and the record claims a human was in the loop
when no human was ever told. A platform that cannot prove it asked is a platform
whose audit trail is a decoration.

These tests assert the row exists, that it names the agent that asked, and that
the auto-approval switch changes *when* a decision happens without changing that
a request was made. They are deliberately written against the database rather
than the return value: a function that returns "an approval was opened" and
writes nothing passes the easier test and fails this one.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import AUTO_APPROVER_ID, TaskExecutionService
from ai_orchestrator.config.settings import reset_settings_cache
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Agent, Approval, Organization, User
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _NeedsApprovalRuntime(ScriptedRuntime):
    """An agent that reaches the point where a person has to decide."""

    name = "needs_approval"

    async def execute(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

        return AgentResult(
            status=AgentResultStatus.NEEDS_APPROVAL,
            summary="the supplier quote is above my spending limit; a person must approve it",
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
    await _ensure_operator(tenant)
    return tenant


async def _ensure_operator(tenant) -> None:  # type: ignore[no-untyped-def]
    """The `users` row an approver id has to point at.

    `approvals.decided_by` is a composite foreign key to `users`, so recording
    *who* decided is not optional — an id with no row behind it makes the write
    fail. The auto-approver is the same principal a person operates as, so the
    row it needs is the operator row, not a service credential.
    """

    existing = (
        await tenant.session.execute(
            select(User).where(
                User.organization_id == tenant.organization_id, User.id == AUTO_APPROVER_ID
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        tenant.session.add(
            User(
                id=AUTO_APPROVER_ID,
                organization_id=tenant.organization_id,
                email="operator@localhost",
                display_name="Local operator",
            )
        )
        await tenant.session.flush()


@pytest_asyncio.fixture
async def approval_task(seeded):  # type: ignore[no-untyped-def]
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    executive = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id, Agent.name == "Executive Agent"
            )
        )
    ).scalar_one()
    task = await tasks.create(
        title="award the HVAC retrofit contract",
        goal="award the HVAC retrofit contract",
        task_type="coordination",
        requester_type="human",
    )
    await tasks.assign(task.id, executive.id)
    return task, executive


async def _approvals(seeded, task_id: str) -> list[Approval]:  # type: ignore[no-untyped-def]
    return list(
        (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == task_id,
                )
            )
        )
        .scalars()
        .all()
    )


class TestAskingForApprovalReachesAPerson:
    async def test_a_run_that_needs_approval_leaves_a_request(self, seeded, approval_task) -> None:  # type: ignore[no-untyped-def]
        """The request exists, names the task, and names who asked.

        Without the row the only evidence is a status, and a status is written by
        the same code that would have to be trusted to have asked.
        """
        task, _executive = approval_task
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_NeedsApprovalRuntime(),
            run_mode=RunMode.SIMULATION,
            auto_approve=False,
        )
        await service.execute_task(task.id)

        rows = await _approvals(seeded, str(task.id))
        assert rows, (
            "the run said it needed approval and nothing was asked; the task is "
            "now waiting on a person who was never told"
        )
        request = rows[0]
        assert request.task_id == str(task.id)
        assert request.reason, "a request with no reason gives a person nothing to decide on"

    async def test_the_request_names_the_agent_that_asked(self, seeded, approval_task) -> None:  # type: ignore[no-untyped-def]
        """The requester is the agent, not the platform.

        An approval requested by the system on the agent's behalf cannot be
        traced back to a decision the agent made, which is the only thing worth
        recording.
        """
        task, executive = approval_task
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_NeedsApprovalRuntime(),
            run_mode=RunMode.SIMULATION,
            auto_approve=False,
        )
        await service.execute_task(task.id)

        request = (await _approvals(seeded, str(task.id)))[0]
        assert request.requested_by == str(executive.id), (
            "the approval must name the agent that asked for it; a requester of "
            "the platform is an approval nobody could audit"
        )

    async def test_auto_approval_still_writes_the_request(self, seeded, approval_task) -> None:  # type: ignore[no-untyped-def]
        """The switch changes the timing, not the fact.

        A demo that auto-approves silently is indistinguishable from a system
        that never asked — and that is the version which cannot later be shown
        to have involved a person. The request is written either way.
        """
        task, _executive = approval_task
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_NeedsApprovalRuntime(),
            run_mode=RunMode.SIMULATION,
            auto_approve=True,
        )
        await service.execute_task(task.id)

        rows = await _approvals(seeded, str(task.id))
        assert rows, "auto-approval must not skip the request; it answers one"
        request = rows[0]
        assert request.status != "pending", (
            "with auto-approval on, the request should already be answered"
        )
        assert (
            "auto" in (request.decision_note or "").lower()
            or "automatic" in (request.decision_note or "").lower()
        ), (
            "an automatic decision has to say it was automatic, or the audit "
            "reads as though a person answered"
        )

    async def test_with_auto_approval_off_the_request_waits(self, seeded, approval_task) -> None:  # type: ignore[no-untyped-def]
        """Off means off: a pending request nobody has answered.

        The switch that makes a demo run unattended is the same switch that could
        quietly remove a human from the loop, so its off position is the one that
        needs a test.
        """
        task, _executive = approval_task
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_NeedsApprovalRuntime(),
            run_mode=RunMode.SIMULATION,
            auto_approve=False,
        )
        await service.execute_task(task.id)

        request = (await _approvals(seeded, str(task.id)))[0]
        assert request.status == "pending", (
            "with the switch off nobody has answered, so the request must still "
            "be waiting rather than recorded as decided"
        )

    def test_the_switch_defaults_to_off(self) -> None:
        """A platform that decides on its own unless told otherwise can be
        talked into deciding on its own.

        The default is the whole safety property, and it is worth one line.
        """
        reset_settings_cache()
        from ai_orchestrator.config.settings import get_settings

        assert get_settings().approval_auto_approve is False
