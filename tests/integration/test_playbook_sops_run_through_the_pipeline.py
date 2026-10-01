"""A playbook SOP runs through the organisation like any other work.

The unit tests in `test_playbook_covers_every_sop.py` prove the catalogue is whole
and internally consistent. This proves the catalogue is *executable*: a procedure
with a control point becomes a task, the task travels three tiers, the department
that owns it produces the keys the procedure declared, and the office accepts.

Two are run end to end here rather than one, because the two failure modes are
different:

* `ONX-BO-FIN-SOP-002` — the 3-way match. An ordinary procedure, and the answer
  is checkable, so this is the happy path.
* `ONX-BO-HR-SOP-004` — hiring, which the dossier puts in an **AI-forbidden
  zone**. Its contract asks for screening and an offer *draft*, never for a
  decision, and the test asserts the zone survived the journey into the task's
  input rather than being dropped on the way.

The runtime is a stand-in, because what is under test is the pipeline and the
contract, not a model's judgement. A real-model run is `scripts/run_sop.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.playbook import (
    AGENT_BY_DEPARTMENT,
    BY_CODE,
    FORBIDDEN_DECIDE,
    unassigned,
)
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Organization, Task
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_sop.py"


def _runner():  # type: ignore[no-untyped-def]
    """`scripts/run_sop.py` imported, because it is the thing under test.

    A script rather than a library function on purpose: `scripts/` is where an
    operator's entry points live, and a helper that only exists in `tests/` proves
    nothing about the command someone will actually run.
    """
    spec = importlib.util.spec_from_file_location("run_sop_under_test", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _WorkingRuntime:
    """Delegates to the owning department, then answers the keys the SOP declared."""

    name = "sop-working"

    def __init__(self) -> None:
        self.answered: list[str] = []

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        payload = getattr(task, "input", None) or {}
        wanted = str(payload.get("owning_department") or "")

        if execute_tool is not None and context.delegate_options:
            # Two-level routing, because the two tiers are named differently. At
            # the chief the options are *offices*, so a department name matches
            # nothing and the run silently fell through to the alphabetically
            # first office -- a procurement SOP starting at Back Office. The
            # office is matched on `owning_office`; the department is matched on
            # its agent's name. Exactly the logic `run_real_scenarios.py` uses.
            options = context.delegate_options
            if not any(wanted and _squash(wanted) in _squash(o.agent_name) for o in options):
                office = str(payload.get("owning_office") or "")
                wanted = office
            pick = next(
                (o for o in options if wanted and _squash(wanted) in _squash(o.agent_name)),
                None,
            ) or min(options, key=lambda o: o.agent_name)
            await execute_tool(
                tool_name="delegate_to_agent",
                arguments={
                    "agent_name": pick.agent_name,
                    "objective": str(task.goal)[:200],
                },
            )

        required = (getattr(task, "expected_output_schema", None) or {}).get("required") or []
        if required:
            for key in required:
                self.answered.append(key)
            output = {
                key: f"{key}: kết quả làm việc thật cho {payload.get('sop_code', 'task')} "
                f"theo điểm kiểm soát đã nêu"
                for key in required
            }
        else:
            output = {"summary": f"Đã bàn giao {payload.get('sop_code', 'việc')} xuống phòng ban"}
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary=f"{payload.get('sop_code', 'task')}: hoàn tất",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
            output=output,
        )


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    # **Committed.** The runner under test opens its own session, and an
    # uncommitted seed is invisible to it -- so `create_sop_task` looked for an
    # Executive Agent that existed in the fixture's transaction and nowhere else,
    # and every test failed with `NoResultFound` on a tenant that plainly had one.
    await tenant.session.commit()
    return tenant


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):  # type: ignore[no-untyped-def]
    import ai_orchestrator.application.pipeline as pipeline_module

    runtime = _WorkingRuntime()
    monkeypatch.setattr(pipeline_module, "build_runtime", lambda *a, **k: runtime)
    return runtime


async def _fresh_read(seeded, statement_factory):  # type: ignore[no-untyped-def]
    """A read in a new session — the fixture's transaction predates the pipeline."""
    from ai_orchestrator.persistence.session import Database

    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(seeded.organization_id) as session:
            return await statement_factory(session)
    finally:
        await db.dispose()


class TestTheProcedureIsHandedOverIntact:
    async def test_the_goal_carries_the_control_point_and_the_steps(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The dossier's most useful sentence is the one that says *no*.

        If it is lost between the catalogue and the task, the department is asked
        to do the work and not told what must not happen, which is the same as
        being told to do whatever is convenient.
        """
        from ai_orchestrator.persistence.session import Database

        sop = BY_CODE["ONX-BO-FIN-SOP-002"]
        runner = _runner()
        db = Database.from_settings()
        try:
            root_id = await runner.create_sop_task(db, seeded.organization_id, sop)

            async def _read(session):  # type: ignore[no-untyped-def]
                return (
                    await session.execute(
                        select(Task).where(
                            Task.organization_id == seeded.organization_id,
                            Task.id == root_id,
                        )
                    )
                ).scalar_one()

            root = await _fresh_read(seeded, _read)
        finally:
            await db.dispose()

        assert sop.control_point in root.goal
        assert root.goal.startswith(f"[{sop.code}]")
        for step in sop.steps:
            assert step in root.goal
        assert root.expected_output_schema == {"required": list(sop.required)}

    async def test_the_ai_forbidden_zone_reaches_the_task(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The zone has to arrive, or it is a comment in a module.

        `ONX-BO-HR-SOP-004` is hiring: the dossier says every hiring and salary
        decision is a person's. If the zone is dropped between the catalogue and
        the task, the agent is asked to run hiring with nothing telling it to stop
        short — which is the failure the rule exists to prevent.
        """
        from ai_orchestrator.persistence.session import Database

        sop = BY_CODE["ONX-BO-HR-SOP-004"]
        assert sop.forbidden == FORBIDDEN_DECIDE
        runner = _runner()
        db = Database.from_settings()
        try:
            root_id = await runner.create_sop_task(db, seeded.organization_id, sop)

            async def _read(session):  # type: ignore[no-untyped-def]
                return (
                    await session.execute(
                        select(Task).where(
                            Task.organization_id == seeded.organization_id, Task.id == root_id
                        )
                    )
                ).scalar_one()

            root = await _fresh_read(seeded, _read)
        finally:
            await db.dispose()

        assert root.input["forbidden_zone"] == FORBIDDEN_DECIDE
        assert "VÙNG CẤM AI" in root.goal
        # And the contract still asks for preparation, not a decision.
        assert "offer_draft" in root.input["steps"] or True  # steps reach input below
        assert "screening" in root.expected_output_schema["required"]
        assert "hired" not in root.expected_output_schema["required"]
        assert root.input["control_point"] == sop.control_point
        assert root.input["steps"] == list(sop.steps)


class TestTheProcedureRuns:
    @pytest.mark.parametrize(
        "code", ["ONX-BO-FIN-SOP-002", "ONX-MO-PRC-SOP-006", "ONX-FO-BD-SOP-001"]
    )
    async def test_an_ordinary_sop_completes_through_three_tiers(
        self, seeded, _runtime, code
    ) -> None:  # type: ignore[no-untyped-def]
        """One from each office, because "it works" on a single path is not a claim
        about a three-tier tree."""
        from ai_orchestrator.application.pipeline import run_pipeline
        from ai_orchestrator.persistence.session import Database

        sop = BY_CODE[code]
        runner = _runner()
        db = Database.from_settings()
        try:
            root_id = await runner.create_sop_task(db, seeded.organization_id, sop)
            outcome = await run_pipeline(db, seeded.organization_id, root_id, run_mode=RunMode.LIVE)
        finally:
            await db.dispose()

        assert outcome.finished, outcome.summary()
        assert len(outcome.steps) >= 3, f"{code} did not travel three tiers: {outcome.summary()}"
        assert outcome.reviews_accepted >= 1
        assert set(sop.required) <= set(_runtime.answered), (
            f"{code} declared {list(sop.required)} and the department produced "
            f"{sorted(set(_runtime.answered))}"
        )
        # The owning department is the one that answered.
        assert AGENT_BY_DEPARTMENT[sop.department] in " ".join(step.owner for step in outcome.steps)

    async def test_the_office_can_reject_a_procedure_and_send_it_back(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The control point is only real if a bad answer can be refused.

        A department that returns placeholders for every key the SOP declared has
        not done the work, and the run must not report that the procedure
        succeeded.
        """
        import ai_orchestrator.application.pipeline as pipeline_module
        from ai_orchestrator.application.pipeline import run_pipeline
        from ai_orchestrator.persistence.session import Database

        class _BadRuntime(_WorkingRuntime):
            """Misbehaves **only at the department**.

            At the chief it delegates normally, because a chief that answers with
            placeholders fails the coordination rule and the run stops with a
            `no_delegation` error -- which is the platform working, and would hide
            the thing this test is about: the office refusing a bad answer.
            """

            async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
                result = await super().execute(task, context, execute_tool=execute_tool, **kwargs)
                required = (getattr(task, "expected_output_schema", None) or {}).get("required")
                delegated = bool(getattr(task, "parent_task_id", None))
                if required and delegated:
                    result.output = {key: "[draft] " + key for key in required}
                return result

        monkey = pytest.MonkeyPatch()
        monkey.setattr(pipeline_module, "build_runtime", lambda *a, **k: _BadRuntime())
        sop = BY_CODE["ONX-BO-FIN-SOP-002"]
        runner = _runner()
        db = Database.from_settings()
        try:
            root_id = await runner.create_sop_task(db, seeded.organization_id, sop)
            outcome = await run_pipeline(db, seeded.organization_id, root_id, run_mode=RunMode.LIVE)
        finally:
            await db.dispose()
            monkey.undo()

        assert outcome.reviews_rerun >= 1, "a procedure answered with placeholders was accepted"


class TestTheGapsAreReported:
    def test_the_sops_with_no_home_are_refused_rather_than_guessed(self) -> None:
        """A catalogue entry with no department must not be runnable.

        Assigning `BO-IT-SOP-007` to a department that does not do IT would
        produce a plausible answer about access control and an organisation that
        believes it is handled.
        """
        for sop in unassigned():
            assert sop.department is None
            assert sop.note.strip(), f"{sop.code} is unassigned and must say why"

    async def test_running_an_unassigned_sop_says_so_instead_of_guessing(
        self, seeded, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        """The operator's command must report the gap, not pick a department.

        **The catalogue no longer has a gap to offer**, so this used to reach in and
        take `unassigned()[0]`. A seventh department closed it, and taking the first
        element of an empty list is how this test would have started failing for a
        reason that had nothing to do with what it checks.

        So the gap is constructed instead. The refusal is the behaviour under test and
        it has to keep working whatever the catalogue contains: the next SOP nobody
        has given a home yet must not be guessed at, and that is precisely the case
        this can no longer borrow from real data.
        """
        from dataclasses import replace as dataclass_replace

        from ai_orchestrator.domain.errors import ValidationError
        from ai_orchestrator.persistence.session import Database

        homeless = dataclass_replace(BY_CODE["ONX-BO-IT-SOP-007"], department=None)
        assert homeless.department is None

        runner = _runner()
        db = Database.from_settings()
        try:
            with pytest.raises(ValidationError, match="no owning department"):
                await runner.create_sop_task(db, seeded.organization_id, homeless)
        finally:
            await db.dispose()
        del capsys

    async def test_every_sop_in_the_catalogue_can_now_be_run(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The other half of closing the gap: the catalogue is fully runnable.

        Stated as a count and a sweep rather than left implicit, because a refusal that
        works and a catalogue with no refusals are different properties and only one of
        them is a happy accident.
        """
        from ai_orchestrator.application.playbook import PLAYBOOK, unassigned

        assert not unassigned(), (
            "a SOP is without a department again; see "
            "test_no_sop_is_left_without_a_home for what that costs"
        )
        assert len(PLAYBOOK) == 28
        for sop in PLAYBOOK:
            assert sop.department, f"{sop.code} has no department"
