"""A coordinator is judged on its coordination, not on the work it forwarded.

Found on the first real free-model run of the three-tier pipeline. The contract
`{"required": ["verdicts", "reason"]}` belongs to the department that does the
work; it propagates down so that department can be held to it -- and then it was
enforced on the **chief**, which delegated and returned. The chief has no verdicts
of its own, so it failed with

    this task said it would produce reason, verdicts, and produced nothing

which is true, and is the wrong question for a task whose job was to route the work
rather than to do it.

The answer a coordinator owes is *what it delegated and what came back*, and that is
composed from the children's accepted outputs by `settle_finished`. It is not
re-derived by the model that only forwarded it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Agent, Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

CONTRACT = {"required": ["verdicts", "reason"]}


class _DelegatingRuntime:
    """Delegates down and returns without producing the worker's own output.

    Which is what a real model did: it called `delegate_to_agent` and its run
    ended there. Under the old rule that failed the run.
    """

    name = "delegating-only"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        payload = getattr(task, "input", None) or {}
        wanted = str(payload.get("owning_department") or "")
        options = context.delegate_options or ()
        if execute_tool is not None and options:
            if not any(wanted and wanted.lower() in o.agent_name.lower() for o in options):
                wanted = str(payload.get("owning_office") or "")
            pick = next(
                (o for o in options if wanted and wanted.lower() in o.agent_name.lower()),
                None,
            ) or min(options, key=lambda o: o.agent_name)
            await execute_tool(
                tool_name="delegate_to_agent",
                arguments={"agent_name": pick.agent_name, "objective": str(task.goal)[:200]},
            )
            return AgentResult(
                status=AgentResultStatus.COMPLETED,
                summary="Đã giao xuống phòng ban",
                execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
                task_id=str(task.task_id),
                # No output at all. That is the point.
            )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="Kết quả: khoản 1 duyệt, khoản 2 trình Giám đốc, khoản 3 từ chối",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
            output={
                "verdicts": "Khoản 1 duyệt không cần trưởng; khoản 2 trình GĐH; khoản 3 từ chối",
                "reason": "Ngưỡng 5.000.000 và 20.000.000 VND theo chính sách chi phí",
            },
        )


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):  # type: ignore[no-untyped-def]
    import ai_orchestrator.application.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: _DelegatingRuntime())


class TestTheCoordinatorIsJudgedOnItsCoordination:
    async def test_a_chief_that_only_delegates_does_not_fail_the_run(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The measured failure, inverted.

        The chief produced no output and was failed for it. Now it produces no
        output and the run finishes, because the answer is composed from the
        department that did the work.
        """
        from ai_orchestrator.application.pipeline import run_pipeline
        from ai_orchestrator.persistence.repositories.task import TaskRepository
        from ai_orchestrator.persistence.session import Database

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        chief = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Executive Agent",
                )
            )
        ).scalar_one()
        root = await tasks.create(
            title="Chấm nhận 3 khoản chi",
            goal="Kiểm tra 3 khoản chi theo chính sách",
            task_type="coordination",
            requester_type="human",
            owner_agent_id=chief.id,
            expected_output_schema=CONTRACT,
            input={
                "owning_department": "Finance Agent",
                "owning_office": "back-office",
            },
        )
        await seeded.session.commit()

        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
        finally:
            await db.dispose()

        assert outcome.finished, (
            f"a chief that delegated was failed for not doing the department's "
            f"work: {outcome.summary()}"
        )

    async def test_the_department_is_still_held_to_the_contract(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """**The other direction, and the one that matters.**

        Relaxing the coordinator must not relax the worker. A department that
        delegated nothing and produced nothing is still failed, because that is the
        whole point of the contract.
        """
        from ai_orchestrator.application.task_execution import TaskExecutionService
        from ai_orchestrator.persistence.models import Task
        from ai_orchestrator.persistence.repositories.task import TaskRepository
        from ai_orchestrator.persistence.session import Database

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        dept = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Finance Agent",
                )
            )
        ).scalar_one()
        task = await tasks.create(
            title="Khoản chi cần kết luận",
            goal="Đối chiếu 3 khoản chi",
            task_type="analysis",
            requester_type="human",
            owner_agent_id=dept.id,
            expected_output_schema=CONTRACT,
        )
        await seeded.session.commit()

        class _Silent(_DelegatingRuntime):
            async def execute(self, t, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
                return AgentResult(
                    status=AgentResultStatus.COMPLETED,
                    summary="xong",
                    execution_id=str(getattr(t, "execution_id", None) or "exec_pending"),
                    task_id=str(t.task_id),
                )

        db = Database.from_settings()
        try:
            async with db.committing_tenant_session(seeded.organization_id) as session:
                service = TaskExecutionService(
                    session=session,
                    organization_id=seeded.organization_id,
                    runtime=_Silent(),
                    run_mode=RunMode.LIVE,
                )
                await service.execute_task(str(task.id))
                await session.commit()
        finally:
            await db.dispose()

        # Read the task row, not the return value: the return is the executor's
        # account of itself and the row is the record a reviewer would read.
        async with db.committing_tenant_session(seeded.organization_id) as session:
            final = (
                await session.execute(select(Task.status).where(Task.id == str(task.id)))
            ).scalar_one()
        assert str(final) == "failed", f"a department that produced nothing finished as {final}"


class TestTheOfficeIsACoordinatorToo:
    """The same mistake, one tier down, and the rule that had not reached it.

    **Measured on a real procurement run, 2026-10-03.** The exemption was written as
    *"a task that delegated is not held to the contract"*, which exempts the chief and
    nothing else. An **office** then answered its own goal instead of handing it to a
    department and was failed three times with

    ```
    category=output_contract_unmet
      reason='this task said it would produce reason, risk, winner, and price and
              produced nothing'
    ```

    `prior_repetitions` went 9 → 10 across the reruns, so the review loop was rejecting
    and repeating the same non-answer rather than converging.

    The rule is now **"can delegate"**, which is what `_delegate_roster` already
    decides: a department has no children and so an empty roster; the chief and each
    office have children and so a non-empty one. These two tests are the two halves.
    """

    async def _run_once(self, seeded, agent_name: str):  # type: ignore[no-untyped-def]
        """Execute one task owned by `agent_name`, with a runtime that produces nothing."""
        from ai_orchestrator.application.task_execution import TaskExecutionService
        from ai_orchestrator.persistence.models import Task
        from ai_orchestrator.persistence.repositories.task import TaskRepository
        from ai_orchestrator.persistence.session import Database

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        owner = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == agent_name,
                )
            )
        ).scalar_one()
        task = await tasks.create(
            title="Kết luận đấu thầu",
            goal="Chọn nhà cung cấp theo tiêu chí",
            task_type="analysis",
            requester_type="human",
            owner_agent_id=owner.id,
            expected_output_schema=CONTRACT,
        )
        await seeded.session.commit()

        class _Silent(_DelegatingRuntime):
            async def execute(self, t, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
                # Produces nothing and delegates nothing: the exact shape that failed
                # an office three times on the real run.
                return AgentResult(
                    status=AgentResultStatus.COMPLETED,
                    summary="Đã xem xét",
                    execution_id=str(getattr(t, "execution_id", None) or "exec_pending"),
                    task_id=str(t.task_id),
                )

        db = Database.from_settings()
        try:
            async with db.committing_tenant_session(seeded.organization_id) as session:
                service = TaskExecutionService(
                    session=session,
                    organization_id=seeded.organization_id,
                    runtime=_Silent(),
                    run_mode=RunMode.LIVE,
                )
                await service.execute_task(str(task.id))
                await session.commit()
            async with db.committing_tenant_session(seeded.organization_id) as session:
                final = (
                    await session.execute(select(Task.status).where(Task.id == str(task.id)))
                ).scalar_one()
        finally:
            await db.dispose()
        return str(final)

    async def test_an_office_that_answers_its_own_goal_is_not_failed(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """An office has departments underneath it, so it is a coordinator."""
        status = await self._run_once(seeded, "Back Office Agent")
        assert status != "failed", (
            "an office was failed for not producing a department's keys, having "
            "answered its own goal instead of delegating to that department"
        )

    async def test_a_department_is_still_failed_for_producing_nothing(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The other half. Relaxing coordinators must not relax the worker.

        Without this the two tests together would pass on a rule that exempts everyone,
        which is the shape a "fix" takes when it is made from one stack trace.
        """
        status = await self._run_once(seeded, "Finance Agent")
        assert status == "failed", (
            f"a department that produced nothing and delegated nothing finished as "
            f"{status}: the output contract is no longer enforced on anyone"
        )
