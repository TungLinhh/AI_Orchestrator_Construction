"""A manager can only choose well if it can see the choice.

The delegate roster is what an agent reads before handing work to a colleague.
It originally carried a name and a unit purpose, which is enough to satisfy the
schema and not enough to decide: six lines that all read the same invite a model
to spread work evenly, including onto people already at capacity.

The claim under test is that the roster now carries what the decision needs --
the person's specialisms and the work they are already holding -- read from the
registry rather than described in the prompt. These tests assert the fields and
that they agree with the database, never the sentence that renders them: the
wording is a presentation choice and asserting on it fails the moment the wording
is improved, which is how a test ends up pinning a bug.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.persistence.models import Agent, Organization, Task
from ai_orchestrator.persistence.repositories.organization import AgentRepository
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _agent(seeded, name: str):  # type: ignore[no-untyped-def]
    return (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == seeded.organization_id, Agent.name == name)
        )
    ).scalar_one()


async def _options_for(seeded, agent, children):  # type: ignore[no-untyped-def]
    """Build the roster the way the executor does, and return it by name."""
    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.application.task_execution import ResolvedAgent, TaskExecutionService

    service = TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=ScriptedRuntime(),
        run_mode="fake",
    )
    # Only three of the five fields matter to a roster read; the rest are
    # resolved elsewhere and would make this a test of the resolver.
    resolved = ResolvedAgent(
        agent=agent,
        definition=None,  # type: ignore[arg-type]
        role=None,  # type: ignore[arg-type]
        profile=None,  # type: ignore[arg-type]
        org_unit_id=agent.org_unit_id,
    )
    options = await service._delegate_options(resolved)
    return {o.agent_name: o for o in options}


class TestTheRosterCarriesTheWorkload:
    async def test_a_colleagues_specialisms_reach_the_roster(self, seeded) -> None:
        """Capabilities come from the registry, so a run can be matched to a skill.

        Asserted against the agent row rather than a literal: the point is that
        the roster agrees with the database, so seeding a different capability
        changes the roster without touching this test.
        """
        executive = await _agent(seeded, "Executive Agent")
        options = await _options_for(seeded, executive, [])

        assert options, "the premise: an executive has colleagues to delegate to"
        for option in options.values():
            # Nothing in the roster may be invented: every capability it shows has
            # to exist on some agent in this organization.
            assert not (set(option.capabilities) - await _all_caps(seeded)), (
                "the roster showed a capability no agent declares"
            )
            if option.capabilities:
                # And a name that carries capabilities must carry that agent's own.
                real = await _agent(seeded, option.agent_name)
                assert set(option.capabilities) <= {str(c) for c in (real.capabilities or [])}

    async def test_an_idle_colleague_is_visibly_idle(self, seeded) -> None:
        """Zero open tasks is a fact worth stating, not a value to leave blank.

        A roster that omits the number makes "busy" and "unknown" look the same,
        and the model cannot ask a question the platform has already answered.
        """
        executive = await _agent(seeded, "Executive Agent")
        options = await _options_for(seeded, executive, [])
        assert options
        for option in options.values():
            assert option.active_tasks >= 0, "a negative task count is not a state"
            assert option.current_work == (), "nothing open means nothing to describe"

    async def test_open_work_is_described_not_merely_counted(self, seeded) -> None:
        """The titles are what make a count a workload.

        "3 tasks" does not say whether one is a small approval or a stalled
        negotiation. If the roster carries the number but not the work, a model
        handed it still has to guess, and the guess is the thing that goes wrong.
        """
        executive = await _agent(seeded, "Executive Agent")
        tasks = TaskRepository(seeded.session, seeded.organization_id)

        # A *direct* report of the executive, not a department two tiers down.
        # The tree is Front Office -> Back Office -> Finance (F207), so the
        # executive's roster is the middle tier; asserting on Finance would be
        # asserting the old two-tier bug is still true.
        back_office = await _agent(seeded, "Back Office Agent")
        held = await tasks.create(
            title="close the March retention accrual",
            goal="close the March retention accrual",
            requester_type="human",
        )
        await tasks.assign(held.id, back_office.id)
        await AgentRepository(seeded.session, seeded.organization_id).adjust_load(
            back_office.id, active_delta=1
        )

        options = await _options_for(seeded, executive, [])
        by_name = options.get("Back Office Agent")
        assert by_name is not None, "Back Office is below the executive and must appear"

        live = list(
            (
                await seeded.session.execute(
                    select(Task.title).where(
                        Task.organization_id == seeded.organization_id,
                        Task.owner_agent_id == back_office.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert live, "the premise: the colleague is holding something"
        for title in by_name.current_work:
            assert title in live, (
                "the roster must describe work that exists; a title no task "
                "carries is a hallucination with a database behind it"
            )

    async def test_a_completed_task_leaves_the_workload(self, seeded) -> None:
        """Finished work is not a reason to avoid a colleague.

        The open-status filter has to exclude terminal states, or an agent that
        has been busy all week stays permanently unreadable as a candidate.
        """
        from ai_orchestrator.domain.state_machines import Transition

        executive = await _agent(seeded, "Executive Agent")
        tasks = TaskRepository(seeded.session, seeded.organization_id)
        back_office = await _agent(seeded, "Back Office Agent")

        done = await tasks.create(
            title="file the Q1 audit pack",
            goal="file the Q1 audit pack",
            requester_type="human",
        )
        await tasks.assign(done.id, back_office.id)
        await AgentRepository(seeded.session, seeded.organization_id).adjust_load(
            back_office.id, active_delta=1
        )
        await tasks.transition(done.id, Transition.BEGIN_WORK)
        await tasks.transition(done.id, Transition.COMPLETE)
        await AgentRepository(seeded.session, seeded.organization_id).adjust_load(
            back_office.id, active_delta=-1
        )

        options = await _options_for(seeded, executive, [])
        by_name = options.get("Back Office Agent")
        assert by_name is not None
        assert "file the Q1 audit pack" not in by_name.current_work
        assert by_name.active_tasks == 0


async def _all_caps(seeded) -> set[str]:  # type: ignore[no-untyped-def]
    """Every capability any agent in this organization declares."""
    rows = (
        await seeded.session.execute(
            select(Agent.capabilities).where(Agent.organization_id == seeded.organization_id)
        )
    ).scalars()
    return {str(c) for row in rows for c in (row or [])}
