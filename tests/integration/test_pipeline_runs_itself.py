"""The organisation runs a goal to its conclusion with nobody asked anything.

Two claims, tested separately because they fail differently:

* **It drives itself.** A goal handed to the chief reaches a conclusion on its
  own, through all three tiers, without a person pressing Run per hop. The
  pre-existing behaviour ran exactly one task per press, so "the pipeline runs"
  was not a claim anything could check.

* **The office catches a bad department and the retry fixes it.** The runtime here
  is a stand-in that produces placeholders on the first call and a real answer on
  the second. That is the exact failure this project shipped -- a `completed` task
  whose output was `[draft]` for every promised key -- and the only way to show the
  loop works is to produce it on purpose and watch it get caught.

The second is the important one. A driver that runs everything to `completed` is
what the code did before, and it reported success.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Organization, Task
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _TieredRuntime:
    """Delegates down the tiers; the bottom produces placeholders until rerun 2.

    One runtime class standing in for three tiers, because what is under test is the
    *pipeline*, not the runtime: it must pick the deepest ready task, so the
    bottom tier is always the one that runs, and the office above it is the one
    that gets to review.

    The placeholder-on-first-attempt behaviour is keyed on the task's `attempt`, so
    a retry genuinely produces different output rather than the loop merely
    re-reading the same bad one.
    """

    name = "tiered"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        attempt = int((getattr(task, "input", None) or {}).get("attempt", 1) or 1)
        # `AgentTask` has no `title` -- the runtime is handed the work, not the
        # row, and reading a field of the row is the mistake this hit first.
        label = (getattr(task, "input", None) or {}).get("stage") or str(task.goal)[:30]
        self.calls.append((label, attempt))

        if execute_tool is not None and context.delegate_options:
            # Route to the colleague that owns the work, by the name the
            # organisation knows, so the run exercises three tiers rather than
            # bouncing between two.
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

        declared = (getattr(task, "expected_output_schema", None) or {}) if task else {}
        required = _required(declared)
        if required:
            payload = {
                key: (
                    f"[draft] {key} — for {getattr(task, 'title', '')}"
                    if attempt < 2
                    else f"{key}: a real finding about {getattr(task, 'title', '')[:40]}, "
                    "with the reasoning that produced it"
                )
                for key in required
            }
        else:
            payload = {
                "summary": (f"Handed {getattr(task, 'title', '')} to the office that owns it")
            }
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=f"done: {getattr(task, 'title', '')} (attempt {attempt})",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
            output=payload,
        )


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _required(schema) -> list[str]:  # type: ignore[no-untyped-def]
    if not isinstance(schema, dict):
        return []
    listed = schema.get("required")
    if isinstance(listed, list):
        return [str(k) for k in listed]
    produces = schema.get("produces")
    if isinstance(produces, str):
        return [produces]
    if isinstance(produces, list):
        return [str(k) for k in produces]
    return []


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _root(  # type: ignore[no-untyped-def]
    seeded, *, goal: str, routing: dict | None = None, contract: dict | None = None
):
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    chief = None
    from ai_orchestrator.persistence.models import Agent

    chief = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Executive Agent",
            )
        )
    ).scalar_one()
    root = await tasks.create(
        title=goal[:60],
        goal=goal,
        task_type="coordination",
        requester_type="human",
        owner_agent_id=chief.id,
        input=routing or {},
        # The contract is declared here and *inherited* down the chain, which is
        # the point: a department cannot be held to a promise nobody passed to it.
        expected_output_schema=contract,
    )
    await seeded.session.commit()
    return root


async def _read_fresh(seeded, statement_factory):  # type: ignore[no-untyped-def]
    """Run a read in a **new** session, after the pipeline has committed.

    The `seeded` fixture holds one transaction open for the whole test, and it
    opened before the pipeline committed. Two assertions in this file read through
    it and saw `None` and `{}` for rows the run had plainly written -- which reads
    as "the platform lost the data" and is really "you are looking at last week's
    snapshot". Every read of something the pipeline produced goes through here.
    """
    from ai_orchestrator.persistence.session import Database

    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(seeded.organization_id) as fresh:
            return await statement_factory(fresh)
    finally:
        await db.dispose()


async def _last_error(seeded, task_id: str) -> str | None:  # type: ignore[no-untyped-def]
    async def _read(session):  # type: ignore[no-untyped-def]
        row = (
            await session.execute(
                select(Task.last_error).where(
                    Task.organization_id == seeded.organization_id, Task.id == task_id
                )
            )
        ).scalar_one_or_none()
        return str(row) if row else None

    return await _read_fresh(seeded, _read)


@pytest.fixture(autouse=True)
def _tiered_runtime(monkeypatch):  # type: ignore[no-untyped-def]
    """Point the pipeline at the tiered stand-in, whatever the environment says.

    Autouse because a pipeline test that quietly used the configured provider would
    be a test of the provider.
    """
    import ai_orchestrator.application.pipeline as pipeline_module

    runtime = _TieredRuntime()
    monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: runtime)
    return runtime


class TestThePipelineDrivesItself:
    async def test_a_goal_reaches_a_conclusion_without_anyone_pressing_run(
        self, seeded, _tiered_runtime
    ) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.session import Database

        root = await _root(
            seeded,
            goal="Chấm nhận 3 khoản chi vượt hạn mức theo chính sách",
            routing={
                "owning_department": "Finance Agent",
                "owning_office": "back-office",
            },
        )
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
        finally:
            await db.dispose()

        assert outcome.finished, outcome.summary()
        # Three tiers, not one: the chief, an office, and a department.
        assert len(outcome.steps) >= 3, outcome.summary()
        assert max(step.depth for step in outcome.steps) >= 2, (
            "the work never reached a department two hops down"
        )

    async def test_the_run_is_audited_as_a_run(self, seeded, _tiered_runtime) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.models import AuditLog
        from ai_orchestrator.persistence.session import Database

        root = await _root(seeded, goal="Rà soát hợp đồng thầu phụ")
        db = Database.from_settings()
        try:
            await run_pipeline(db, seeded.organization_id, str(root.id))
            # A **fresh** session, not the fixture's. The fixture's transaction
            # opened before the pipeline committed, so it can be looking at a
            # snapshot from before the run -- which would report the audit row
            # missing for a run that wrote it.
            async with db.committing_tenant_session(seeded.organization_id) as fresh:
                rows = (
                    (
                        await fresh.execute(
                            select(AuditLog.action).where(
                                AuditLog.organization_id == seeded.organization_id,
                                AuditLog.action == "pipeline.run",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await db.dispose()
        assert "pipeline.run" in rows, "a run that changed the organisation left no record"


class TestTheOfficeCatchesBadWork:
    async def test_a_placeholder_department_is_sent_back_and_the_retry_fixes_it(
        self, seeded, _tiered_runtime
    ) -> None:  # type: ignore[no-untyped-def]
        """The whole loop, end to end, on purpose.

        The first attempt writes `[draft]` for every promised key. The office must
        reject it, order the retry, and accept the retry -- and the run must end
        with the root complete *and* with a rejection on the record. A run that
        merely completed would be the old behaviour.
        """
        from ai_orchestrator.persistence.session import Database

        root = await _root(
            seeded,
            goal="Chấm nhận 3 khoản chi vượt hạn mức theo chính sách",
            routing={
                "owning_department": "Finance Agent",
                "owning_office": "back-office",
            },
            contract={"required": ["verdicts", "reason"]},
        )
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), run_mode=RunMode.LIVE
            )
        finally:
            await db.dispose()

        assert outcome.reviews_rerun >= 1, (
            "a department that produced only placeholders was accepted: " + outcome.summary()
        )
        assert outcome.reviews_accepted >= 1, "the retry was never accepted"
        assert outcome.finished, outcome.summary()

        # The retry is a distinct execution, so the loop really ran twice.
        attempts = [attempt for _title, attempt in _tiered_runtime.calls]
        assert 2 in attempts, f"the department only ran once: {_tiered_runtime.calls}"

    async def test_the_budgeted_loop_terminates(self, seeded, _tiered_runtime) -> None:  # type: ignore[no-untyped-def]
        """A run must end. Bounded per work, and bounded overall."""
        from ai_orchestrator.persistence.session import Database

        root = await _root(
            seeded,
            goal="Chấm nhận 3 khoản chi vượt hạn mức theo chính sách",
            routing={
                "owning_department": "Finance Agent",
                "owning_office": "back-office",
            },
        )
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), max_executions=12
            )
        finally:
            await db.dispose()
        assert len(outcome.steps) <= 12
        assert outcome.stopped_because or outcome.finished


class TestItStopsRatherThanSpins:
    async def test_a_goal_nobody_owns_stops_with_a_reason(self, seeded, _tiered_runtime) -> None:  # type: ignore[no-untyped-def]
        """A goal with no agent must say so, not spin to the execution bound.

        A goal naming a department that does not exist is *not* the test for this:
        the stand-in runtime falls back to the least-loaded colleague, so it gets
        routed somewhere and finishes -- which is correct behaviour for a manager
        who must not stall on a bad name. The unambiguous case is work with no
        owner at all, where nothing can be run and the run has to say so.
        """
        from ai_orchestrator.persistence.repositories.task import TaskRepository
        from ai_orchestrator.persistence.session import Database

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        orphan = await tasks.create(
            title="Không ai nhận",
            goal="Một việc không có phòng ban nào nhận",
            task_type="execution",
            requester_type="human",
        )
        await seeded.session.commit()
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(orphan.id), max_executions=6
            )
        finally:
            await db.dispose()
        assert not outcome.finished
        # The refusal is recorded **on the task**, which is where a reader will
        # look -- not only in the driver's summary. Asserting the reason means a
        # run that silently gave up would fail here.
        assert len(outcome.steps) == 1, "an unowned task was executed by nobody"
        assert outcome.steps[0].status == "failed"
        assert "no agent is assigned" in ((await _last_error(seeded, str(orphan.id))) or ""), (
            "the refusal did not say what was wrong"
        )


class _NeverConforms(_TieredRuntime):
    """Delegates down the tiers and the bottom tier *never* stops sending placeholders.

    The counterpart to `_TieredRuntime`, for the case the retry budget exists for:
    a department that has been told exactly what is wrong, twice, and still cannot
    answer. Producing it on purpose is the only way to assert on what the
    organisation does at the bound.
    """

    name = "never-conforms"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        attempt = int((getattr(task, "input", None) or {}).get("attempt", 1) or 1)
        label = (getattr(task, "input", None) or {}).get("stage") or str(task.goal)[:30]
        self.calls.append((label, attempt))

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
                arguments={"agent_name": pick.agent_name, "objective": str(task.goal)[:200]},
            )

        declared = (getattr(task, "expected_output_schema", None) or {}) if task else {}
        required = _required(declared)
        # No attempt ever produces a real answer. `[draft]` on every promised key.
        payload = (
            {key: f"[draft] {key} — chưa xác định" for key in required}
            if required
            else {"summary": "Handed it to the office that owns it"}
        )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=f"done: {getattr(task, 'title', '')} (attempt {attempt})",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending_00000000000000"),
            task_id=str(task.task_id),
            output=payload,
        )


class TestEscalationIsAnAnswerNotAHang:
    """A department that cannot do the work must produce a *conclusion*.

    Three separate defects met in the same scenario, and none of them was visible
    while the retry loop was working:

    * the run ended `running` with `stopped because: nothing is runnable and no gate
      can be cleared`, on the stated policy that "the executive is then told the work
      is unresolved" — the executive was told nothing;
    * fixing that hang by failing the office made the **root** settle `completed`,
      because a coordinator's review asks "did you coordinate" and never looks at
      whether the coordinator itself finished;
    * and then the root's office above it *re-dispatched* that failed office, which
      turned a 4-task run into a 7-task one.

    So: the department is sent back exactly once, escalates at the bound, the office
    fails with the department's reason, the root fails with the office's, and
    nothing is retried on the way up.
    """

    @pytest.fixture(autouse=True)
    def _never(self, monkeypatch):  # type: ignore[no-untyped-def]
        import ai_orchestrator.application.pipeline as pipeline_module

        rt = _NeverConforms()
        monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: rt)
        return rt

    async def _run(self, seeded, rt):  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.session import Database

        root = await _root(
            seeded,
            goal="Một việc mà phòng ban dưới sẽ không bao giờ làm đúng",
            routing={"owning_department": "Finance Agent", "owning_office": "back-office"},
            contract={"required": ["verdicts", "reason"]},
        )
        db = Database.from_settings()
        try:
            return root, await run_pipeline(
                db, seeded.organization_id, str(root.id), max_executions=20
            )
        finally:
            await db.dispose()

    async def test_the_run_ends_failed_rather_than_hanging(self, seeded) -> None:  # type: ignore[no-untyped-def]
        _root, outcome = await self._run(seeded, None)  # type: ignore[arg-type]

        assert outcome.root_status == "failed", (
            "a run over a department that never conformed did not end in a "
            f"conclusion: {outcome.summary()}"
        )
        assert not outcome.finished, "the run reported success over work it could not do"
        assert not outcome.stopped_because, (
            f"the run hung instead of concluding: {outcome.summary()}"
        )

    async def test_the_department_is_sent_back_exactly_once(self, seeded) -> None:  # type: ignore[no-untyped-def]
        _, outcome = await self._run(seeded, None)  # type: ignore[arg-type]

        assert outcome.reviews_rerun == 1, (
            f"the department was sent back {outcome.reviews_rerun} time(s): {outcome.summary()}"
        )
        assert outcome.reviews_escalated == 1, (
            f"the bound did not escalate exactly once: {outcome.summary()}"
        )

    async def test_nothing_above_the_failure_is_retried(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A task that has already failed is never sent back.

        The office whose department escalated goes up as a failure. Re-dispatching
        it produced seven tasks for the same work; the bound that exists to stop the
        loop was being applied one tier too low, so the failure never bound.
        """
        _root, outcome = await self._run(seeded, None)  # type: ignore[arg-type]
        assert len(outcome.steps) <= 5, (
            f"the failure was retried upward, so the run grew: {outcome.summary()}"
        )

    async def test_the_root_carries_the_reason_the_department_gave(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The executive must be able to read *why*, several tiers down.

        A failure with no reason is the same as the hang in a different shape: the
        run concludes, and nobody can act on it.
        """
        root, _outcome = await self._run(seeded, None)  # type: ignore[arg-type]
        reason = await _last_error(seeded, str(root.id)) or ""

        assert reason.strip(), "the root failed without saying why"
        assert "placeholder" in reason or "not accepted" in reason, (
            f"the root's reason does not reach the department's finding: {reason!r}"
        )

    async def test_the_whole_tree_is_terminal(self, seeded) -> None:  # type: ignore[no-untyped-def]
        root, _ = await self._run(seeded, None)  # type: ignore[arg-type]
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        async def _subtree(session):  # type: ignore[no-untyped-def]
            return await TaskRepository(session, seeded.organization_id).subtree_statuses(
                str(root.id)
            )

        statuses = await _read_fresh(seeded, _subtree)
        stuck = {
            k: v
            for k, v in statuses.items()
            if v not in {"completed", "failed", "canceled", "expired"}
        }
        assert not stuck, f"the run concluded with work still open: {stuck}"


class TestTheTreeIsReal:
    async def test_every_task_below_the_root_is_in_the_run(self, seeded, _tiered_runtime) -> None:  # type: ignore[no-untyped-def]
        """The pipeline must not leave an orphan behind and call it finished.

        A root that is `completed` while a department task below it is still
        `created` is the failure mode of a driver that only looks at the root.
        """
        from ai_orchestrator.persistence.session import Database

        root = await _root(
            seeded,
            goal="Chấm nhận 3 khoản chi vượt hạn mức theo chính sách",
            routing={
                "owning_department": "Finance Agent",
                "owning_office": "back-office",
            },
        )
        db = Database.from_settings()
        try:
            outcome = await run_pipeline(
                db, seeded.organization_id, str(root.id), max_executions=12
            )
        finally:
            await db.dispose()

        from ai_orchestrator.persistence.repositories.task import TaskRepository

        # Through the repository's own subtree walk, **not** `root_task_id`. That
        # column is written as `parent_task_id`, so it names the immediate parent
        # and a filter on it finds nothing two tiers down -- which is how this
        # assertion first read "the run created no child tasks at all" on a run
        # that had created two.
        async def _subtree(session):  # type: ignore[no-untyped-def]
            return await TaskRepository(session, seeded.organization_id).subtree_statuses(
                str(root.id)
            )

        statuses = await _read_fresh(seeded, _subtree)
        statuses.pop(str(root.id), None)
        assert statuses, "the run created no child tasks at all"
        if outcome.finished:
            assert all(
                s in {"completed", "failed", "canceled", "expired"} for s in statuses.values()
            ), f"the root finished with work still open below it: {statuses}"
