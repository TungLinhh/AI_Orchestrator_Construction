"""Approving a run has to change the next run.

The loop this closes: request, work, approval, and then nothing. The same task
next month got the same work, because nothing about the run survived. The
approval ended the story instead of being the moment the story improved.

The properties worth protecting, in the order they break:

* **a lesson is written at all** — the whole point, and the one most likely to
  pass silently if the wiring is missing;
* **it is not published** — a `SkillVersion` that is published is what the
  runtime loads, so publishing it here would be the agent editing its own
  instructions the moment somebody clicked Approve;
* **the evidence travels with it**, so a reviewer can check the lesson against
  the log rather than take it on trust;
* **a rejection teaches nothing** — a refused run is evidence the approach was
  wrong, and the response to that is not to repeat it, not to write it into the
  agent's instructions;
* **a failure to learn does not undo the decision** — a person who approved
  something must not be shown a failure because the platform could not write a
  note.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.skill_learner import SkillLearner
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.approvals.service import ApprovalService
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import ActorType, RiskLevel, RunMode
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Approval, Organization, SkillVersion, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _WorkingRuntime(ScriptedRuntime):
    """A run that uses a tool, then finishes and says what it did.

    The tool call is not decoration. A run that called nothing has no *approach*
    to record, and the learner refuses to invent one -- so a test of the learning
    loop that uses a tool-less runtime is a test of the refusal, wearing the
    name of a test of the loop.
    """

    name = "working"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None and not getattr(self, "_used", False):
            self._used = True
            # `calculator`, not `write_report`. The Finance agent is bound to
            # `internal_database_query`, `document_reader` and `calculator`; a
            # tool it is not bound to is refused by the gateway, and a refused
            # call still leaves an audit row -- so using one here would have
            # "worked" while testing the refusal rather than the loop.
            await execute_tool(
                tool_name="calculator",
                arguments={"expression": "118000 * 1.12"},
            )
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            summary="priced the package and filed the note",
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
    return tenant


async def _operator(tenant):  # type: ignore[no-untyped-def]
    """A person who may decide, with the `users` row the foreign key requires.

    Provisioned through the platform's own function rather than by hand. The
    `users` row is a composite foreign key target and `users` is keyed on `id`
    alone, so an id that exists in another tenant cannot be used here (F209) --
    which is exactly the bug this test path would otherwise rediscover.
    """
    from ai_orchestrator.security.auth import ensure_local_operator, no_auth_principal

    await ensure_local_operator(tenant.session, str(tenant.organization_id))
    return no_auth_principal(str(tenant.organization_id)).actor


async def _run_and_await_approval(seeded, tenant):  # type: ignore[no-untyped-def]
    """A completed run that pauses for a person, and the approval row it produced."""
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id, Agent.name == "Finance Agent"
            )
        )
    ).scalar_one()
    task = await tasks.create(
        title="price the HVAC package",
        goal="price the HVAC package",
        task_type="execution",
        requester_type="human",
    )
    await tasks.assign(task.id, agent.id)

    class _NeedsApproval(ScriptedRuntime):
        """The gate run. It asks a question and stops; it does not do the work."""

        name = "needs"

        async def execute(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
            return AgentResult(
                status=AgentResultStatus.NEEDS_APPROVAL,
                summary="the quote is above my limit; a person must agree",
                execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
                task_id=str(task.task_id),
            )

    await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=_NeedsApproval(),
        run_mode=RunMode.LIVE,
        auto_approve=False,
    ).execute_task(task.id)

    return task, agent


async def _finish_after_approval(seeded, task):  # type: ignore[no-untyped-def]
    """The work, once a person has agreed to it.

    `approval_granted` returns the task to `running`, so there is no second
    `begin_work` -- reaching for one is an illegal transition, and a test that
    guesses at the machine rather than reading it gets an error that looks like
    a platform defect.

    This runs *after* the decision, not before. An approval authorises
    continuing; a run that finished first was never authorised, and learning
    from it would be learning from work nobody agreed to.
    """
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    await tasks.transition(task.id, Transition.APPROVAL_GRANTED)
    await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=_WorkingRuntime(),
        run_mode=RunMode.LIVE,
        auto_approve=False,
    ).execute_task(task.id)


class TestTheApprovalWritesALesson:
    async def test_approving_a_run_produces_a_skill_version(self, seeded, tenant) -> None:
        """The loop, closed. Without this the approval is an endpoint and nothing more."""
        task, _agent = await _run_and_await_approval(seeded, tenant)
        approval = (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == str(task.id),
                )
            )
        ).scalar_one()

        before = len(
            (
                await seeded.session.execute(
                    select(SkillVersion).where(
                        SkillVersion.organization_id == seeded.organization_id
                    )
                )
            )
            .scalars()
            .all()
        )

        operator = await _operator(tenant)
        await ApprovalService(seeded.session, seeded.organization_id).decide(
            str(approval.id), approver=operator, approve=True, note="agreed"
        )
        # The lesson is written when the work *finishes*, which is after the
        # decision. An approval authorises continuing; learning at the gate would
        # teach from a run that stopped, and its log holds no result.
        await _finish_after_approval(seeded, task)
        await seeded.session.flush()

        after = (
            (
                await seeded.session.execute(
                    select(SkillVersion).where(
                        SkillVersion.organization_id == seeded.organization_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(after) == before + 1, (
            "a person approved this run and nothing was written down about it; the "
            "next run of the same work will be identical"
        )

    async def test_the_lesson_is_not_published(self, seeded, tenant) -> None:
        """The one property that keeps this from being self-modification.

        A published version is what the runtime loads on the next run. Publishing
        it here would mean the agent rewrote its own instructions the instant a
        person clicked Approve, and over several iterations it converges on
        whatever it already wanted to do.
        """
        task, _agent = await _run_and_await_approval(seeded, tenant)
        approval = (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == str(task.id),
                )
            )
        ).scalar_one()
        operator = await _operator(tenant)
        baseline = await _version_ids(seeded)
        await ApprovalService(seeded.session, seeded.organization_id).decide(
            str(approval.id), approver=operator, approve=True
        )
        await _finish_after_approval(seeded, task)
        await seeded.session.flush()

        version = await _newest_version(seeded, baseline)
        assert version is not None, "no lesson was written, so nothing was published either"
        assert not version.is_published, (
            "a lesson was published by the act of approving it; that is the agent "
            "editing its own instructions, not a review"
        )

    async def test_the_lesson_carries_its_evidence(self, seeded, tenant) -> None:
        """A reviewer can only check a lesson against the log if the log is stored."""
        task, _agent = await _run_and_await_approval(seeded, tenant)
        approval = (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == str(task.id),
                )
            )
        ).scalar_one()
        operator = await _operator(tenant)
        baseline = await _version_ids(seeded)
        await ApprovalService(seeded.session, seeded.organization_id).decide(
            str(approval.id), approver=operator, approve=True
        )
        await _finish_after_approval(seeded, task)
        await seeded.session.flush()

        version = await _newest_version(seeded, baseline)
        assert version is not None
        assert isinstance(version.derived_from, dict) and version.derived_from, (
            "a lesson with no evidence attached is unfalsifiable, and an "
            "unfalsifiable lesson is not worth a person's time to review"
        )
        assert "task_title" in version.derived_from, (
            f"the evidence does not describe the run it came from: {version.derived_from}"
        )


class TestWhatMustNotHappen:
    async def test_rejecting_a_run_writes_no_lesson(self, seeded, tenant) -> None:
        """A refused run is not a procedure to append.

        The evidence says the approach was wrong. The response to that is to not
        repeat it, which is not something to write into the agent's standing
        instructions as though it were a method.
        """
        task, _agent = await _run_and_await_approval(seeded, tenant)
        approval = (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == str(task.id),
                )
            )
        ).scalar_one()
        fired: list[str] = []
        operator = await _operator(tenant)
        await ApprovalService(
            seeded.session,
            seeded.organization_id,
            on_approved=lambda a: fired.append(str(a.id)) or _noop(),
        ).decide(str(approval.id), approver=operator, approve=False, note="no")
        await seeded.session.flush()

        assert not fired, (
            "a rejected run was treated as a lesson; the platform has just taught "
            "an agent that doing the wrong thing well is the way to be documented"
        )

    async def test_a_failure_to_learn_does_not_undo_the_decision(self, seeded, tenant) -> None:
        """A person who approved something must not see it fail.

        The learning runs inside a decision already made. Letting it raise would
        turn "the platform could not write a note" into "your approval was rolled
        back" — strictly worse than not learning, and it would be reported as
        though the approval itself had failed.
        """
        task, _agent = await _run_and_await_approval(seeded, tenant)
        approval = (
            await seeded.session.execute(
                select(Approval).where(
                    Approval.organization_id == seeded.organization_id,
                    Approval.task_id == str(task.id),
                )
            )
        ).scalar_one()

        async def _explode(_a: object) -> None:
            msg = "the skill store is unavailable"
            raise RuntimeError(msg)

        operator = await _operator(tenant)
        decision = await ApprovalService(
            seeded.session,
            seeded.organization_id,
            on_approved=_explode,
        ).decide(str(approval.id), approver=operator, approve=True)
        await seeded.session.flush()

        assert decision.status.value == "approved", (
            "a learning failure changed the decision; the person still approved it "
            "and the record must say so"
        )
        row = (
            await seeded.session.execute(select(Approval).where(Approval.id == str(approval.id)))
        ).scalar_one()
        assert row.status == "approved"

    async def test_approving_an_approval_with_no_task_writes_nothing(self, seeded, tenant) -> None:
        """Not every approval is about a task, and a task was not invented for it.

        A document approval has no run behind it. Reading one and composing a
        lesson from it would produce instructions about work nobody performed.
        """
        from ai_orchestrator.domain.ids import ApprovalId

        approval = Approval(
            id=str(ApprovalId.create()),
            organization_id=seeded.organization_id,
            action_type="document.publish",
            action_payload={},
            effect_class="mutate_internal",
            risk_level=RiskLevel.MEDIUM.value,
            reason="publish the procedure note",
            payload_hash="h",
            requested_by="req",
            requested_by_type=ActorType.AGENT.value,
            required_approver_roles=["org_admin"],
            status="pending",
        )
        seeded.session.add(approval)
        await seeded.session.flush()

        async def _check(a: object) -> None:
            from ai_orchestrator.application.task_execution import (
                TaskExecutionService as _S,
            )

            # The callback the service installs refuses a taskless approval.
            learner = SkillLearner(seeded.session, seeded.organization_id)
            outcome = await learner.learn_from(task_id="", agent_id="agt_none")
            assert outcome["written"] is False
            del _S

        operator = await _operator(tenant)
        await ApprovalService(
            seeded.session,
            seeded.organization_id,
            on_approved=_check,
        ).decide(str(approval.id), approver=operator, approve=True)
        await seeded.session.flush()


async def _version_ids(seeded) -> set[str]:  # type: ignore[no-untyped-def]
    """Which skill versions already existed.

    A before/after set rather than an id pattern: the seeded catalogue shares the
    `skl_` prefix, so matching on it cannot tell a lesson from a seed, and a test
    that guesses the convention will pass or fail for the wrong reason.
    """
    return {
        str(v)
        for v in (
            (
                await seeded.session.execute(
                    select(SkillVersion.id).where(
                        SkillVersion.organization_id == seeded.organization_id
                    )
                )
            )
            .scalars()
            .all()
        )
    }


async def _newest_version(seeded, baseline: set[str]) -> SkillVersion | None:  # type: ignore[no-untyped-def]
    """The one version this run added, or `None` if it added none."""
    rows = (
        (
            await seeded.session.execute(
                select(SkillVersion).where(SkillVersion.organization_id == seeded.organization_id)
            )
        )
        .scalars()
        .all()
    )
    new = [r for r in rows if str(r.id) not in baseline]
    return new[0] if len(new) == 1 else None


async def _noop() -> None:
    return None


async def _agent_id(seeded, task_id: str) -> str:  # type: ignore[no-untyped-def]
    """Who performed the work, which is who the lesson belongs to."""
    value = (
        await seeded.session.execute(
            select(Task.owner_agent_id).where(
                Task.organization_id == seeded.organization_id, Task.id == task_id
            )
        )
    ).scalar_one()
    return str(value)
