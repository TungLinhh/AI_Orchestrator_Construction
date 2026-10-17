"""Chain-B pipeline level: a human gate pauses the run honestly, never forged.

Request-level tests pin that an approval row is written; these pin the driver
behaviour through `run_pipeline` with `auto_approve=False`: the outcome names
the waiting task, the run stops with a waiting reason, and no approval is
granted or invented along the way. Fake runtime only, no model, no network.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.persistence.models import Approval, Organization, Task
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _NeedsApprovalRuntime:
    """An agent that reaches the point where a person has to decide."""

    name = "chainb-needs-approval"

    async def execute(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

        return AgentResult(
            status=AgentResultStatus.NEEDS_APPROVAL,
            summary="the quote is above my spending limit; a person must approve it",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
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


@pytest.fixture
def _gated_runtime(monkeypatch):  # type: ignore[no-untyped-def]
    import ai_orchestrator.application.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: _NeedsApprovalRuntime())


async def _root(seeded):  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.models import Agent
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    chief = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Executive Agent",
            )
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    root = await tasks.create(
        title="Duyet bao gia vuot han muc",
        goal="Duyet bao gia vuot han muc chi tieu",
        task_type="coordination",
        requester_type="human",
        owner_agent_id=chief.id,
    )
    await seeded.session.commit()
    return root


async def _fresh_status(seeded, task_id: str) -> str:  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.session import Database

    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(seeded.organization_id) as fresh:
            return str(
                (
                    await fresh.execute(
                        select(Task.status).where(
                            Task.organization_id == seeded.organization_id,
                            Task.id == task_id,
                        )
                    )
                ).scalar_one()
            )
    finally:
        await db.dispose()


async def _approvals_for(seeded, task_id: str) -> list[Approval]:  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.session import Database

    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(seeded.organization_id) as fresh:
            return list(
                (
                    await fresh.execute(
                        select(Approval).where(
                            Approval.organization_id == seeded.organization_id,
                            Approval.task_id == task_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await db.dispose()


class TestTheGatePausesTheRun:
    async def test_waiting_task_is_named_and_the_run_stops_waiting(
        self, seeded, _gated_runtime
    ) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.session import Database

        root = await _root(seeded)
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), auto_approve=False
            )
        finally:
            await db.dispose()

        assert str(root.id) in outcome.waiting_for_human, (
            "the gate paused but the outcome names nobody: " + outcome.summary()
        )
        assert not outcome.finished, "a run paused on a human gate reported completion"
        assert "waiting" in outcome.stopped_because or "person" in outcome.stopped_because, (
            f"the stop reason does not say it is waiting: {outcome.stopped_because!r}"
        )
        assert await _fresh_status(seeded, str(root.id)) == "waiting_for_approval"

    async def test_no_approval_is_granted_or_invented(self, seeded, _gated_runtime) -> None:  # type: ignore[no-untyped-def]
        """Negative: the pause is honest — one pending request, zero decisions."""
        from ai_orchestrator.persistence.session import Database

        root = await _root(seeded)
        db = Database.from_settings()
        try:
            await run_pipeline(db, seeded.organization_id, str(root.id), auto_approve=False)
            second = await run_pipeline(
                db, seeded.organization_id, str(root.id), auto_approve=False
            )
        finally:
            await db.dispose()

        rows = await _approvals_for(seeded, str(root.id))
        assert rows, "the task waits but nobody was ever asked"
        assert {r.status for r in rows} == {"pending"}, (
            f"a gate answered itself: {[r.status for r in rows]}"
        )
        assert len(rows) == 1, f"re-running a waiting gate duplicated the request: {len(rows)} rows"
        assert str(root.id) in second.waiting_for_human, "the gate did not survive a second drain"
