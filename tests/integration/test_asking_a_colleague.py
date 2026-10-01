"""Asking a colleague must not become handing them work.

The distinction this file protects is the one between a question and a
delegation. They look alike from the caller's side — both name a colleague and
hand over a sentence — and collapsing them has consequences that are visible
long after the run: a colleague's queue fills with work nobody asked them to
own, a manager sees unfinished tasks that were never anyone's, and the audit
trail says a transfer happened when none did.

So the tests assert the two things that would break first if that line moved:
asking leaves no task and no delegation, and asking stops. Both are asserted as
"what must not happen", which is worth more than a positive assertion here: a
test that only checks the happy path passes just as happily when the tool has
become a delegation with extra steps.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.peer_consultation import (
    CONSULT_LIMIT_PER_RUN,
    REFUSAL_LIMIT,
    REFUSAL_NOT_ADDRESSABLE,
    build_query,
    can_consult,
)
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Delegation, Organization, Task

pytestmark = pytest.mark.integration


class _AnsweringRuntime(ScriptedRuntime):
    """Asks a colleague, then finishes. Mirrors how the bridge does it."""

    name = "asking"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None and not getattr(self, "_already_asked", False):
            self._already_asked = True
            self.ask_result = await execute_tool(
                tool_name="ask_agent",
                arguments={
                    "agent_name": "Back Office Agent",
                    "question": "is this within the travel policy?",
                },
            )
        return await super().execute(task, context, execute_tool=None, **kwargs)


class _LoopingRuntime(ScriptedRuntime):
    """Asks forever. The ceiling is what stops it, and this proves it does."""

    name = "looping"

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[str] = []
        self.refusals: list[str] = []

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            while True:
                result = await execute_tool(
                    tool_name="ask_agent",
                    # A *direct* report of the executive. The tree is three tiers
                    # (F207), so Finance sits two hops down and is not in this
                    # roster at all -- asking for it would be refused before the
                    # ceiling was ever reached, and the loop would end for the
                    # wrong reason and prove nothing.
                    arguments={"agent_name": "Back Office Agent", "question": "and now?"},
                )
                kind = result.error_kind or ""
                if kind in (REFUSAL_LIMIT, REFUSAL_NOT_ADDRESSABLE):
                    self.refusals.append(kind)
                    break
                self.answers.append(str(result.output))
        return await super().execute(task, context, execute_tool=None, **kwargs)


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    from ai_orchestrator.seed import seed

    await seed(tenant.session, into=org)
    return tenant


async def _counts(seeded) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    tasks = (
        await seeded.session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.organization_id == seeded.organization_id)
        )
    ).scalar_one()
    delegations = (
        await seeded.session.execute(
            select(func.count())
            .select_from(Delegation)
            .where(Delegation.organization_id == seeded.organization_id)
        )
    ).scalar_one()
    return int(tasks), int(delegations)


class TestAskingIsNotDelegating:
    async def test_an_ask_creates_no_task_and_no_delegation(self, seeded) -> None:
        """The load-bearing assertion.

        If a consultation ever writes a task, a colleague is made responsible for
        something nobody assigned them, and every downstream count of open work
        becomes wrong in a way no single test would notice.
        """
        from ai_orchestrator.application.task_execution import TaskExecutionService
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        tasks_repo = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks_repo.create(
            title="settle the Q1 travel claim",
            goal="settle the Q1 travel claim",
            task_type="coordination",
            requester_type="human",
        )
        from sqlalchemy import select as _select

        from ai_orchestrator.persistence.models import Agent as _Agent

        executive = (
            await seeded.session.execute(
                _select(_Agent).where(
                    _Agent.organization_id == seeded.organization_id,
                    _Agent.name == "Executive Agent",
                )
            )
        ).scalar_one()
        await tasks_repo.assign(parent.id, executive.id)

        # Counted after the fixture's own work, so the difference is the run's.
        before_tasks, before_delegations = await _counts(seeded)

        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_AnsweringRuntime(),
            run_mode=RunMode.SIMULATION,
        )
        await service.execute_task(parent.id)

        after_tasks, after_delegations = await _counts(seeded)
        assert after_tasks == before_tasks, (
            "a consultation must leave no task; a colleague was given work nobody "
            "assigned them, and the organisation now counts it as open"
        )
        assert after_delegations == before_delegations, (
            "a consultation is not a delegation; a delegation row here would put a "
            "false transfer in the audit trail"
        )

    async def test_the_ceiling_stops_a_loop(self, seeded) -> None:
        """A model that asks in a circle is stopped by a counter, not by a hope.

        The prompt asks for restraint. This is the mechanism that does not
        depend on restraint being granted.
        """
        from ai_orchestrator.application.task_execution import TaskExecutionService
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        tasks_repo = TaskRepository(seeded.session, seeded.organization_id)
        parent = await tasks_repo.create(
            title="reconcile the Q1 ledger",
            goal="reconcile the Q1 ledger",
            task_type="coordination",
            requester_type="human",
        )
        from sqlalchemy import select as _select

        from ai_orchestrator.persistence.models import Agent as _Agent

        executive = (
            await seeded.session.execute(
                _select(_Agent).where(
                    _Agent.organization_id == seeded.organization_id,
                    _Agent.name == "Executive Agent",
                )
            )
        ).scalar_one()
        await tasks_repo.assign(parent.id, executive.id)

        runtime = _LoopingRuntime()
        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=runtime,
            run_mode=RunMode.SIMULATION,
        )
        await service.execute_task(parent.id)

        assert len(runtime.answers) <= CONSULT_LIMIT_PER_RUN, (
            f"the ceiling is {CONSULT_LIMIT_PER_RUN} consultations and "
            f"{len(runtime.answers)} got through"
        )
        assert runtime.refusals, (
            "the loop has to end in a refusal the model can read; a run that "
            "simply stops asking has been cut off, which reads as an answer"
        )


class TestAddressability:
    def test_only_the_roster_is_addressable(self) -> None:
        """One list decides who may be asked, and it is the roster.

        A second list derived from the org tree would be a second vocabulary,
        and the two would drift apart the moment either changed.
        """
        from ai_orchestrator.domain.contracts import DelegateOption

        roster = (
            DelegateOption(agent_name="Finance Agent", purpose="money"),
            DelegateOption(agent_name="HR Agent", purpose="people"),
        )
        assert can_consult(roster, "Finance Agent")
        assert not can_consult(roster, "Procurement Agent")
        assert not can_consult((), "Finance Agent")

    def test_a_nobody_is_not_addressable(self) -> None:
        """An agent with no subordinates has nobody to ask.

        The refusal is the answer. Returning an empty roster and letting the
        model infer "no one is available" is how an invented colleague gets
        proposed anyway.
        """
        assert not can_consult((), "anyone")

    def test_the_question_carries_its_context(self) -> None:
        """The recipient must know what it is being asked about.

        A bare question invites an answer about the wrong problem. Asserted on
        the two facts that have to survive into the prompt — the caller's work
        and the ceiling on the reply — rather than on the sentence, because the
        wording is a presentation choice.
        """
        query = build_query("settle the Q1 travel claim", "is this within policy?")
        assert "settle the Q1 travel claim" in query
        assert "is this within policy?" in query
        assert "not a transfer of work" in query, (
            "the recipient has to know nobody is loading it onto them, or it "
            "answers as though it were"
        )


class TestRefusalsNameTheirCause:
    def test_the_limit_refusal_names_the_ceiling(self) -> None:
        """A model told only "refused" retries; told the ceiling was hit, it stops.

        The reason is the actionable part of a refusal, so it is worth testing
        that the constant the tool returns is one a caller can act on.
        """
        assert REFUSAL_LIMIT == "CONSULT_LIMIT_REACHED"
        assert REFUSAL_NOT_ADDRESSABLE == "AGENT_NOT_AVAILABLE"
        assert REFUSAL_LIMIT != REFUSAL_NOT_ADDRESSABLE, (
            "two different refusals sharing a code would make the loop above "
            "unstoppable: the model would see the same word every time"
        )
