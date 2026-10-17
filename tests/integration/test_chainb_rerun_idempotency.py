"""Chain-B pipeline level: a rejection reviewed twice orders exactly one rerun.

`review_office_work` is documented as safe to call repeatedly because the
pipeline driver calls it after every run. The existing suite pins this for a
*pending* retry; these tests pin it for a *finished* tree (the second review
sees the same finished rejection) and for the loop's end (an accepted retry
closes the work without a third task). All assertions read task rows, not the
review's return value alone.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.work_review import review_office_work
from ai_orchestrator.persistence.models import Organization, Task
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

CONTRACT = {"required": ["ket_luan", "ly_do"]}
GOOD = {
    "ket_luan": "Khoan 1 duyet, khoan 2 trinh Giam doc, khoan 3 tu choi vi thieu hop dong.",
    "ly_do": "Ap dung nguong chi phi theo chinh sach.",
}
PLACEHOLDERS = {
    "ket_luan": "[draft] ket_luan - chua xac dinh",
    "ly_do": "[draft] ly_do - chua xac dinh",
}


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _tree(seeded, *, work_key: str = "chain-b:finance"):  # type: ignore[no-untyped-def]
    """One office task, one finished-but-rejected department task, delegation row."""
    from ai_orchestrator.persistence.models import Agent
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
        title="Cham nhan 3 khoan chi",
        goal="Kiem tra 3 khoan chi theo chinh sach",
        task_type="coordination",
        requester_type="human",
        owner_agent_id=office.id,
    )
    child = await tasks.create(
        title="Cham nhan 3 khoan chi",
        goal="Kiem tra 3 khoan chi theo chinh sach",
        task_type="analysis",
        parent_task_id=office_task.id,
        owner_agent_id=dept.id,
        requester_type="agent",
        requester_agent_id=office.id,
        expected_output_schema=CONTRACT,
        input={
            "work_key": work_key,
            "attempt": 1,
            "owning_department": "Finance Agent",
            "brief": "Chinh sach: duyet theo nguong.",
        },
    )
    child.output = PLACEHOLDERS
    child.status = "completed"
    await DelegationRepository(seeded.session, org).record(
        parent_task_id=str(office_task.id),
        source_agent_id=str(office.id),
        target_agent_id=str(dept.id),
        objective="Kiem tra 3 khoan chi theo chinh sach",
        path=_path(str(office.id), str(office_task.id)),
        platform_limits=_limits(),
        parent_limits=_limits(),
        child_task_id=str(child.id),
    )
    await seeded.session.flush()
    return office_task, child


def _path(agent_id: str, task_id: str):  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.delegation import DelegationPath
    from ai_orchestrator.domain.ids import AgentId, TaskId

    return DelegationPath.root(AgentId(agent_id), TaskId(task_id))


def _limits():  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.delegation import DelegationLimits

    return DelegationLimits.platform_default()


async def _reruns_of(seeded, child_id: str) -> list[Task]:  # type: ignore[no-untyped-def]
    rows = (
        (
            await seeded.session.execute(
                select(Task).where(Task.organization_id == seeded.organization_id)
            )
        )
        .scalars()
        .all()
    )
    return [t for t in rows if isinstance(t.input, dict) and t.input.get("rerun_of") == child_id]


class TestARejectionReviewedTwiceOrdersOneRerun:
    async def test_two_reviews_of_one_finished_rejection_order_exactly_one_rerun(
        self, seeded
    ) -> None:  # type: ignore[no-untyped-def]
        office_task, child = await _tree(seeded)

        first = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert len(first.rerun) == 1

        second = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert second.rerun == [], "the same finished rejection was ordered twice"

        reruns = await _reruns_of(seeded, str(child.id))
        assert len(reruns) == 1, f"expected exactly one rerun row, found {len(reruns)}"

    async def test_two_distinct_rejections_order_two_reruns(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Negative: the 'exactly one' above is not a creator that only fires once."""
        from ai_orchestrator.persistence.models import Agent
        from ai_orchestrator.persistence.repositories.task import (
            DelegationRepository,
            TaskRepository,
        )

        office_task, first_child = await _tree(seeded, work_key="chain-b:finance-a")
        org = seeded.organization_id
        office_id = str(first_child.requester_agent_id)
        dept_id = str(first_child.owner_agent_id)
        tasks = TaskRepository(seeded.session, org)
        second = await tasks.create(
            title="Cham nhan 3 khoan chi (viec khac)",
            goal="Kiem tra 3 khoan chi theo chinh sach",
            task_type="analysis",
            parent_task_id=office_task.id,
            owner_agent_id=dept_id,
            requester_type="agent",
            requester_agent_id=office_id,
            expected_output_schema=CONTRACT,
            input={"work_key": "chain-b:finance-b", "attempt": 1},
            allow_parallel=True,
        )
        second.output = dict(PLACEHOLDERS)
        second.status = "completed"
        office_agent = (
            await seeded.session.execute(select(Agent).where(Agent.id == office_id))
        ).scalar_one()
        dept_agent = (
            await seeded.session.execute(select(Agent).where(Agent.id == dept_id))
        ).scalar_one()
        await DelegationRepository(seeded.session, org).record(
            parent_task_id=str(office_task.id),
            source_agent_id=str(office_agent.id),
            target_agent_id=str(dept_agent.id),
            objective="viec thu hai",
            path=_path(str(office_agent.id), str(office_task.id)),
            platform_limits=_limits(),
            parent_limits=_limits(),
            child_task_id=str(second.id),
        )
        await seeded.session.flush()

        outcome = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert len(outcome.rerun) == 2, (
            "two distinct rejected pieces of work must each be sent back once"
        )


class TestAnAcceptedRetryClosesTheWork:
    async def test_finishing_the_rerun_well_adds_no_third_task(self, seeded) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        office_task, child = await _tree(seeded)
        first = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        rerun_id = first.rerun[0].rerun_task_id
        assert rerun_id is not None

        rerun = await TaskRepository(seeded.session, seeded.organization_id).get(rerun_id)
        rerun.output = GOOD
        rerun.status = "completed"
        await seeded.session.flush()

        second = await review_office_work(
            seeded.session, seeded.organization_id, str(office_task.id)
        )
        assert second.all_accepted, "a repaired retry must close the work"
        assert len(await _reruns_of(seeded, str(child.id))) == 1
