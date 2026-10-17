"""Chain-B pipeline level: restart resumes, cancel does not duplicate.

Two driver properties the suite only had pieces of:

* **restart**: a drain stopped early (execution bound) resumes on the next
  `run_pipeline` and settles; a drain over an already finished tree executes
  nothing new.
* **cancel**: cancelling the driver mid-execution leaves no duplicate
  settlement behind — a rerun settles each task once and finishes the root.

Fake runtimes only, no model, no network. Execution counts are read from the
`executions` table in a fresh session, never from the driver's own counters.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Execution, Organization, Task
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _required(schema) -> list[str]:  # type: ignore[no-untyped-def]
    if not isinstance(schema, dict):
        return []
    listed = schema.get("required")
    if isinstance(listed, list):
        return [str(k) for k in listed]
    return []


class _ChainRuntime:
    """Delegates down the chain and produces conforming answers at the bottom."""

    name = "chainb-restart"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(str(task.task_id))
        if execute_tool is not None and context.delegate_options:
            wanted = str((getattr(task, "input", None) or {}).get("owning_department") or "")
            pick = next(
                (
                    o
                    for o in context.delegate_options
                    if wanted and _squash(wanted) in _squash(o.agent_name)
                ),
                None,
            )
            if pick is None:
                pick = min(context.delegate_options, key=lambda o: o.agent_name)
            await execute_tool(
                tool_name="delegate_to_agent",
                arguments={
                    "agent_name": pick.agent_name,
                    "objective": str(task.goal)[:200],
                },
            )
        required = _required(getattr(task, "expected_output_schema", None) or {})
        payload = (
            {k: f"{k}: ket luan thuc te co ly do kem theo" for k in required}
            if required
            else {"summary": f"Da chuyen xuong van phong phu trach {task.task_id}"}
        )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=f"done: {task.task_id}",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
            output=payload,
        )


class _GatedRuntime(_ChainRuntime):
    """Blocks inside the first execution until released; cancellable."""

    name = "chainb-gated"

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        self.entered.set()
        await self.release.wait()
        return await super().execute(task, context, execute_tool=execute_tool, **kwargs)


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
def _chain_runtime(monkeypatch):  # type: ignore[no-untyped-def]
    import ai_orchestrator.application.pipeline as pipeline_module

    runtime = _ChainRuntime()
    monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: runtime)
    return runtime


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
        title="Cham nhan 3 khoan chi vuot han muc",
        goal="Cham nhan 3 khoan chi vuot han muc theo chinh sach",
        task_type="coordination",
        requester_type="human",
        owner_agent_id=chief.id,
        input={"owning_department": "Finance Agent", "owning_office": "back-office"},
        expected_output_schema={"required": ["ket_luan", "ly_do"]},
    )
    await seeded.session.commit()
    return root


async def _execution_count(seeded, task_id: str) -> int:  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.session import Database

    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(seeded.organization_id) as fresh:
            return int(
                (
                    await fresh.execute(
                        select(func.count())
                        .select_from(Execution)
                        .where(
                            Execution.organization_id == seeded.organization_id,
                            Execution.task_id == task_id,
                        )
                    )
                ).scalar_one()
            )
    finally:
        await db.dispose()


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


class TestRestartResumes:
    async def test_a_bounded_drain_resumes_and_settles(self, seeded, _chain_runtime) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.session import Database

        root = await _root(seeded)
        db = Database.from_settings()
        try:
            partial = await run_pipeline(
                db,
                seeded.organization_id,
                str(root.id),
                max_executions=1,
                run_mode=RunMode.LIVE,
            )
            assert not partial.finished, "one execution must not finish a three-tier goal"
            assert len(partial.steps) == 1
            assert await _fresh_status(seeded, str(root.id)) == "running"

            resumed = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
        finally:
            await db.dispose()

        assert resumed.finished, resumed.summary()
        assert await _fresh_status(seeded, str(root.id)) == "completed"
        # The root ran once (in the partial drain); the resume settled it
        # without executing it again.
        assert await _execution_count(seeded, str(root.id)) == 1, (
            "the resume re-executed the root instead of settling it"
        )

    async def test_a_finished_tree_reruns_with_no_new_executions(
        self, seeded, _chain_runtime
    ) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.session import Database

        root = await _root(seeded)
        db = Database.from_settings()
        try:
            first = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
            assert first.finished, first.summary()
            real_steps = [s for s in first.steps if not s.orphan]
            assert real_steps, "the first run executed nothing; the rerun proves nothing"

            second = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
        finally:
            await db.dispose()

        assert [s for s in second.steps if not s.orphan] == [], (
            f"a rerun over a finished tree executed work: {second.summary()}"
        )
        assert await _fresh_status(seeded, str(root.id)) == "completed"


class TestCancelIsReportedAndNotReplayed:
    """Cancelling the driver mid-execution is answered by the attempt owner.

    Measured behaviour (`application/task_attempt.py:_interrupt`): an
    interrupted attempt fails its execution with
    `error_category="attempt_interrupted"`, moves a `running` task to
    `blocked` with `reconciliation_required` for inspection, and audits
    `task.attempt.interrupted` — deliberately **not** replayed blindly. The
    pipeline-level contract is therefore: the cancel surfaces
    (`CancelledError`), the interruption is recorded (not swallowed), and a
    rerun replays nothing and settles nothing twice.
    """

    async def _cancel_once(self, seeded, monkeypatch):  # type: ignore[no-untyped-def]
        import ai_orchestrator.application.pipeline as pipeline_module
        from ai_orchestrator.persistence.session import Database

        runtime = _GatedRuntime()
        monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: runtime)
        root = await _root(seeded)
        db = Database.from_settings()
        try:
            driver = asyncio.create_task(run_pipeline(db, seeded.organization_id, str(root.id)))
            await asyncio.wait_for(runtime.entered.wait(), timeout=30)
            driver.cancel()
            with pytest.raises(asyncio.CancelledError):
                await driver
            runtime.release.set()
            return str(root.id), runtime
        finally:
            await db.dispose()

    async def test_cancel_is_reported_not_swallowed(self, seeded, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.models import AuditLog
        from ai_orchestrator.persistence.session import Database

        root_id, _ = await self._cancel_once(seeded, monkeypatch)

        db = Database.from_settings()
        try:
            async with db.committing_tenant_session(seeded.organization_id) as fresh:
                task = (
                    await fresh.execute(
                        select(Task).where(
                            Task.organization_id == seeded.organization_id,
                            Task.id == root_id,
                        )
                    )
                ).scalar_one()
                execs = (
                    (
                        await fresh.execute(
                            select(Execution).where(
                                Execution.organization_id == seeded.organization_id,
                                Execution.task_id == root_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                audits = (
                    (
                        await fresh.execute(
                            select(AuditLog.action).where(
                                AuditLog.organization_id == seeded.organization_id,
                                AuditLog.action == "task.attempt.interrupted",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await db.dispose()

        assert task.status == "blocked", (
            f"an interrupted run must park the task for inspection, not leave it {task.status}"
        )
        assert (task.constraints or {}).get("reconciliation_required") is True
        assert len(execs) == 1, f"cancel must not multiply executions: {len(execs)}"
        assert execs[0].status == "failed"
        assert execs[0].error_category == "attempt_interrupted"
        assert audits, "the interruption left no audit row; it was swallowed"

    async def test_blocked_work_is_not_silently_replayed(self, seeded, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Negative: a rerun after cancel replays nothing and settles nothing twice."""
        import ai_orchestrator.application.pipeline as pipeline_module
        from ai_orchestrator.persistence.session import Database

        root_id, _ = await self._cancel_once(seeded, monkeypatch)

        db = Database.from_settings()
        try:
            runtime2 = _ChainRuntime()
            monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: runtime2)
            outcome = await run_pipeline(db, seeded.organization_id, root_id)
        finally:
            await db.dispose()

        assert [s for s in outcome.steps if not s.orphan] == [], (
            f"blocked work was replayed without inspection: {outcome.summary()}"
        )
        assert outcome.settled == [] and outcome.settled_failed == [], (
            "a rerun over interrupted work settled something"
        )
        assert await _execution_count(seeded, root_id) == 1
        assert await _fresh_status(seeded, root_id) == "blocked"
