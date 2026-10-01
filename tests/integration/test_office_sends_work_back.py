"""The office sends work back when a department does it badly, and does not when it does not.

The unit tests in `test_office_review.py` prove the rules. These prove the two
things only a database can show: that a rejection produces a **real** second task
addressed to the same department carrying the findings, and that an acceptance
produces nothing at all.

The failure being tested is the one this project shipped: a department task marked
`completed` whose output was a placeholder for every key it had promised. Nothing
downstream could tell. The office now can, and the proof is a row in `tasks` with
the findings in its brief.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.work_review import (
    ACTION_ACCEPTED,
    attempt_of,
    review_office_work,
    work_key,
)
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

CONTRACT = {"required": ["verdicts", "reason"]}
GOOD = {
    "verdicts": (
        "Khoản 1 duyệt không cần trưởng; khoản 2 trình Giám đốc điều hành; "
        "khoản 3 từ chối vì thiếu hợp đồng thuê kho."
    ),
    "reason": "Áp dụng ngưỡng 5.000.000 và 20.000.000 VND theo chính sách chi phí.",
}
PLACEHOLDERS = {
    "verdicts": "[draft] verdicts — for Chấm nhận 3 khoản chi",
    "reason": "[draft] reason — for Chấm nhận 3 khoản chi",
}


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    """A tenant with the real three-tier organisation in it.

    Seeded rather than hand-built because the test is about the *middle* tier: an
    office with two departments under it only exists if the seed builds it, and a
    hand-built pair would pass whether or not the organisation does.
    """
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _tree(
    seeded, *, office_name: str = "Back Office Agent", dept_name: str = "Finance Agent"
):  # type: ignore[no-untyped-def]
    """An office, a department under it, and one piece of work between them."""
    from ai_orchestrator.persistence.models import Agent
    from ai_orchestrator.persistence.repositories.task import (
        DelegationRepository,
        TaskRepository,
    )

    org = seeded.organization_id
    office = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org, Agent.name == office_name)
        )
    ).scalar_one()
    dept = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org, Agent.name == dept_name)
        )
    ).scalar_one()

    tasks = TaskRepository(seeded.session, org)
    office_task = await tasks.create(
        title="Chấm nhận 3 khoản chi",
        goal="Kiểm tra 3 khoản chi theo chính sách",
        task_type="coordination",
        requester_type="human",
        owner_agent_id=office.id,
    )
    child = await tasks.create(
        title="Chấm nhận 3 khoản chi",
        goal="Kiểm tra 3 khoản chi theo chính sách",
        task_type="analysis",
        parent_task_id=office_task.id,
        owner_agent_id=dept.id,
        requester_type="agent",
        requester_agent_id=office.id,
        expected_output_schema=CONTRACT,
        input={
            "work_key": "expense-policy:finance",
            "attempt": 1,
            "owning_department": "Finance Agent",
            "brief": "Chính sách: dưới 5.000.000 duyệt trưởng; trên 20.000.000 trình GĐH.",
        },
    )
    await DelegationRepository(seeded.session, org).record(
        parent_task_id=str(office_task.id),
        source_agent_id=str(office.id),
        target_agent_id=str(dept.id),
        objective="Kiểm tra 3 khoản chi theo chính sách",
        path=_path(str(office.id), str(office_task.id)),
        platform_limits=_limits(),
        parent_limits=_limits(),
        child_task_id=str(child.id),
    )
    await seeded.session.flush()
    return office_task, child, office, dept


def _path(agent_id: str, task_id: str):  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.delegation import DelegationPath
    from ai_orchestrator.domain.ids import AgentId, TaskId

    return DelegationPath.root(AgentId(agent_id), TaskId(task_id))


def _limits():  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.delegation import DelegationLimits

    return DelegationLimits.platform_default()


class TestGoodWorkIsAccepted:
    async def test_a_contract_met_is_accepted_and_nothing_is_created(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The loop's first duty is to *not* interfere.

        An office that re-sends work a department did correctly is the failure that
        would make the whole mechanism worse than useless, so it is asserted
        directly: no new task, and an acceptance in the audit trail.
        """
        office_task, child, _office, _dept = await _tree(seeded)
        child.output = GOOD
        child.status = "completed"
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )

        assert outcome.all_accepted
        assert [r.action for r in outcome.reviewed] == ["accepted"]
        assert outcome.rerun == []
        assert not outcome.waiting

        rows = (
            (
                await seeded.session.execute(
                    select(child.__class__).where(
                        child.__class__.organization_id == seeded.organization_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2, "a review created a task for work that was accepted"

    async def test_the_acceptance_is_in_the_audit_trail(self, seeded) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.models import AuditLog

        office_task, child, _office, _dept = await _tree(seeded)
        child.output = GOOD
        child.status = "completed"
        await seeded.session.flush()
        await review_office_work(seeded.session, seeded.organization_id, str(office_task.id))

        actions = (
            (
                await seeded.session.execute(
                    select(AuditLog.action).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.action == ACTION_ACCEPTED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert ACTION_ACCEPTED in actions


class TestBadWorkIsSentBack:
    async def test_placeholders_produce_a_real_second_task(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The whole point: `[draft]` output that was marked `completed` is caught.

        Every earlier check passes -- the promised keys are all present -- so only
        the placeholder check catches it. That is why it is in the module.
        """
        office_task, child, office, dept = await _tree(seeded)
        child.output = PLACEHOLDERS
        child.status = "completed"
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )

        assert len(outcome.rerun) == 1
        rejection = outcome.rerun[0]
        assert rejection.task_id == str(child.id)
        assert rejection.rerun_task_id

        from ai_orchestrator.persistence.models import Task

        rerun = (
            await seeded.session.execute(select(Task).where(Task.id == rejection.rerun_task_id))
        ).scalar_one()

        assert str(rerun.owner_agent_id) == str(dept.id), "sent back to the wrong department"
        assert str(rerun.requester_agent_id) == str(office.id), "the office did not order it"
        # `assigned`, not `created`: the retry is addressed to the department the
        # moment it exists, so the driver picks it up without anyone assigning it.
        assert rerun.status == "assigned"
        # The findings travel as the brief, or the rerun is a rerun of the same mistake.
        assert "PHẢI LÀM LẠI" in rerun.goal
        assert "placeholder" in rerun.goal
        assert rerun.expected_output_schema == CONTRACT, "the contract was lost on the retry"
        assert attempt_of(rerun) == 2
        assert work_key(rerun) == work_key(child), "the retry looks like new work"
        # **The material comes with the retry.** The first version sent a department
        # back to repeat the work with `work_key`, `attempt` and the findings -- and
        # nothing else, so the work was stripped out and the retry was guaranteed to
        # fail for the reason the first attempt did. Measured on a real free-model
        # run: the original task had the brief and a 1043-character goal, the retry
        # had neither, and the office escalated a piece of work nobody had been
        # given a second time.
        assert rerun.input.get("owning_department") == child.input.get("owning_department")
        assert rerun.input.get("brief") == child.input.get("brief")
        assert rerun.input.get("review"), "the findings did not travel with the retry"

    async def test_missing_keys_are_sent_back_too(self, seeded) -> None:  # type: ignore[no-untyped-def]
        office_task, child, _office, _dept = await _tree(seeded)
        child.output = {"verdicts": "Khoản 1 duyệt, khoản 2 trình GĐH, khoản 3 từ chối"}
        child.status = "completed"
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert len(outcome.rerun) == 1
        assert "reason" in outcome.rerun[0].verdict.as_text()

    async def test_a_failed_department_run_is_sent_back_not_ignored(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A crash is not "nothing to review".

        Treating a `failed` department task as unjudgeable would make a broken
        department look idle, and the office would report upward with a hole where
        an answer should be.
        """
        office_task, child, _office, _dept = await _tree(seeded)
        child.output = None
        child.status = "failed"
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert len(outcome.rerun) == 1


class TestTheBoundHolds:
    async def test_the_third_attempt_escalates_instead_of_asking_again(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A manager who rejects forever is not managing either.

        Built from scratch rather than by deleting a task out of a seeded tree: a
        `delegations` row points at the child task, so removing one violates
        `fk_delegations_child_task_id_tasks` -- which is the database reminding us
        that a delegation is a record, not a pointer that can be quietly dropped.
        """
        from ai_orchestrator.persistence.models import Agent, Task
        from ai_orchestrator.persistence.repositories.task import (
            DelegationRepository,
            TaskRepository,
        )

        org = seeded.organization_id
        office = (
            await seeded.session.execute(
                select(Agent).where(Agent.organization_id == org, Agent.name == "Back Office Agent")
            )
        ).scalar_one()
        dept = (
            await seeded.session.execute(
                select(Agent).where(Agent.organization_id == org, Agent.name == "Finance Agent")
            )
        ).scalar_one()
        tasks = TaskRepository(seeded.session, org)
        office_task = await tasks.create(
            title="Chấm nhận 3 khoản chi",
            goal="Kiểm tra 3 khoản chi theo chính sách",
            task_type="coordination",
            requester_type="human",
            owner_agent_id=office.id,
        )
        for attempt in (1, 2):
            t = await tasks.create(
                title=f"attempt {attempt}",
                goal="Kiểm tra 3 khoản chi theo chính sách",
                task_type="analysis",
                parent_task_id=office_task.id,
                owner_agent_id=dept.id,
                requester_type="agent",
                requester_agent_id=office.id,
                expected_output_schema=CONTRACT,
                input={"work_key": "expense-policy:finance", "attempt": attempt},
                allow_parallel=True,
            )
            t.output = PLACEHOLDERS
            t.status = "completed"
            await DelegationRepository(seeded.session, org).record(
                parent_task_id=str(office_task.id),
                source_agent_id=str(office.id),
                target_agent_id=str(dept.id),
                objective=f"attempt {attempt}",
                path=_path(str(office.id), str(office_task.id)),
                platform_limits=_limits(),
                parent_limits=_limits(),
                child_task_id=str(t.id),
            )
        await seeded.session.flush()

        outcome = await review_office_work(seeded.session, org, str(office_task.id), max_attempts=2)

        assert outcome.rerun == [], "a bound of 2 must not produce a third attempt"
        assert len(outcome.escalated) == 1
        assert "escalating" in outcome.escalated[0].why
        assert not outcome.all_accepted

        children = (
            (
                await seeded.session.execute(
                    select(Task.id).where(
                        Task.organization_id == org, Task.parent_task_id == office_task.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 2, "an escalation created another attempt"


class TestCallingItTwiceChangesNothing:
    async def test_a_second_review_of_the_same_finished_work_adds_no_task(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The driver calls this after every run, so idempotence is a requirement.

        Not an optimisation: a driver that reviewed on each pass would otherwise
        double every rejection and spend the retry budget in one pass.
        """
        from ai_orchestrator.persistence.models import Task

        office_task, child, _office, _dept = await _tree(seeded)
        child.output = PLACEHOLDERS
        child.status = "completed"
        await seeded.session.flush()

        first = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert len(first.rerun) == 1
        rerun_id = first.rerun[0].rerun_task_id

        # The retry is now a child too, and it is not finished, so the second
        # review must leave it waiting rather than judge or re-order it.
        second = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert rerun_id in second.waiting
        assert len(second.rerun) == 0, "a pending retry was judged before it ran"

        count = (
            (
                await seeded.session.execute(
                    select(Task.id).where(
                        Task.organization_id == seeded.organization_id,
                        Task.parent_task_id == office_task.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 2, "a second review created a third task for the same work"


class TestAnUnfinishedDepartmentIsNotJudged:
    async def test_a_running_child_is_waiting_not_rejected(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """An agent cannot review an answer that does not exist yet."""
        office_task, child, _office, _dept = await _tree(seeded)
        child.status = "running"
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert outcome.waiting == [str(child.id)]
        assert outcome.reviewed == []
        assert not outcome.all_accepted
