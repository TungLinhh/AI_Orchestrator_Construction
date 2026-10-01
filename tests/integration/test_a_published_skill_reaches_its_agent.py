"""A learned skill has to reach the agent, or the loop is a diary.

The first version of this wrote a `SkillVersion` and stopped. Measured on the
development tenant: four learned versions, eighteen bindings, and **no binding
pointing at any of the learned ones**. The platform reported the loop as working
and every agent learned nothing, because publication is not what applies a skill.

What applies a skill is the binding. `TaskExecutionService._skill_contracts`
joins `agent_skill_bindings` to the version the binding *pins*, and filters on
`is_enabled`. A published version nobody is bound to is a document.

So these tests hold three things in place, and the third is the one that matters:

* publishing pins the version to the agent named in its own evidence;
* the pinned version is what the next run actually receives as instructions;
* a skill with no agent in its evidence is reported, not published silently --
  because a green tick for a change nobody receives is the failure this whole
  file exists to prevent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.skill_learner import SkillLearner
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import (
    Agent,
    AgentSkillBinding,
    Organization,
    SkillVersion,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


class _UsingRuntime(ScriptedRuntime):
    """A run that uses a tool, so there is a method worth learning."""

    name = "using"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None and not getattr(self, "_used", False):
            self._used = True
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


async def _learned(seeded) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    """One completed run, one lesson written about it. Returns ids."""
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Finance Agent",
            )
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="price the HVAC package",
        goal="price the HVAC package",
        task_type="execution",
        requester_type="human",
    )
    await tasks.assign(task.id, agent.id)
    await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=_UsingRuntime(),
        run_mode=RunMode.LIVE,
        auto_approve=False,
    ).execute_task(task.id)

    outcome = await SkillLearner(seeded.session, seeded.organization_id).learn_from(
        task_id=str(task.id), agent_id=str(agent.id)
    )
    await seeded.session.flush()
    assert outcome.get("written"), f"the learner refused: {outcome}"
    return str(task.id), str(agent.id), str(outcome["skill_id"])


async def _publish(seeded, skill_id: str, *, version_id: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    """Publish through the real endpoint logic, not a helper.

    The binding is created inside that logic, so a test that publishes by other
    means would pass while the product stayed inert.
    """
    from ai_orchestrator.api.deps import ApiContext
    from ai_orchestrator.api.skills_tools import PublishSkillRequest, publish_skill

    # The real principal rather than a hand-built `Actor`. The operator id is
    # derived from the organisation (F209): one id belongs to one tenant, and a
    # fixed one cannot satisfy the composite foreign key a decision records
    # against.
    from ai_orchestrator.security.auth import (
        ensure_local_operator,
        no_auth_principal,
    )

    await ensure_local_operator(seeded.session, str(seeded.organization_id))
    principal = no_auth_principal(str(seeded.organization_id))
    ctx = ApiContext(
        principal=principal,
        session=seeded.session,
        organization_id=seeded.organization_id,
        actor=principal.actor,
    )
    return await publish_skill(
        skill_id,
        PublishSkillRequest(
            test_results={"passed": True, "checked": "the lesson reads as instructions"},
            governance_state="active",
        ),
        ctx,
    )


class TestPublishingReachesTheAgent:
    async def test_publishing_pins_the_version_to_the_agent(self, seeded) -> None:
        """The step that makes the loop a loop rather than a diary."""
        _task_id, agent_id, skill_id = await _learned(seeded)

        await _publish(seeded, skill_id)

        binding = (
            await seeded.session.execute(
                select(AgentSkillBinding).where(
                    AgentSkillBinding.organization_id == seeded.organization_id,
                    AgentSkillBinding.agent_id == agent_id,
                    AgentSkillBinding.skill_id == skill_id,
                )
            )
        ).scalar_one_or_none()
        assert binding is not None, (
            "the skill was published and no agent was bound to it; the version is "
            "a document and the next run will not see it"
        )
        assert binding.skill_version_id is not None, (
            "a binding with no pinned version resolves to whatever the join finds, "
            "which is nothing -- and that reads the same as an unbound skill"
        )

    async def test_the_next_run_receives_the_lesson_as_instructions(self, seeded) -> None:
        """The end of the loop, and the only thing that proves it closed.

        Not "a row exists". The lesson has to arrive in the context the agent is
        built with, or the agent is the same agent it was before it learned.
        """
        task_id, agent_id, skill_id = await _learned(seeded)
        await _publish(seeded, skill_id)

        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_UsingRuntime(),
            run_mode=RunMode.SIMULATION,
        )
        # No lookup of the task or the agent row: `_learned` returned their ids
        # after writing them, so re-reading them proves only that the row exists,
        # which is what `_learned` already asserted.
        contracts = await service._skill_contracts(agent_id)
        pinned = [c for c in contracts if str(c.skill_id) == skill_id]
        assert pinned, (
            f"the published lesson is not among the agent's {len(contracts)} skills; "
            "the loop ends at a row nobody reads"
        )
        assert "From the approved run" in pinned[0].instructions, (
            "the skill is bound but carries no lesson; the binding points at a "
            "version whose text is empty"
        )
        del task_id

    async def test_an_unbound_skill_is_not_claimed_as_delivered(self, seeded) -> None:
        """A skill with no agent is reported, not published into silence.

        Publishing it would return a green tick for a change nobody receives,
        which is exactly the shape of bug this platform keeps being bitten by: a
        record that says something happened and nobody checking what.
        """
        _task_id, agent_id, skill_id = await _learned(seeded)

        version = (
            await seeded.session.execute(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id)
            )
        ).scalar_one()
        # Strip the agent, the way a skill a person wrote would look.
        version.derived_from = {
            k: v for k, v in (version.derived_from or {}).items() if k != "agent_id"
        }
        await seeded.session.flush()

        result = await _publish(seeded, skill_id)
        assert result.get("bound_to_agent") is None, (
            "a skill with no agent in its evidence reported a binding; somebody's "
            "instructions would change that they never saw"
        )
        binding = (
            (
                await seeded.session.execute(
                    select(AgentSkillBinding).where(
                        AgentSkillBinding.organization_id == seeded.organization_id,
                        AgentSkillBinding.skill_id == skill_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        assert binding is None, "nothing was bound, and something claims otherwise"
        del agent_id


class TestTheEvidenceIsNotATestResult:
    async def test_the_lesson_does_not_claim_its_tests_passed(self, seeded) -> None:
        """Separate columns, and this is why.

        `test_results` is what the publication gate checks. Parking the run log
        there made every learned skill unpublishable, and the only route to
        publishing one was to fabricate `passed: true` -- inventing the artefact a
        reviewer trusts most.
        """
        _task_id, _agent_id, skill_id = await _learned(seeded)
        version = (
            await seeded.session.execute(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id)
            )
        ).scalar_one()
        assert not (version.test_results or {}).get("passed"), (
            "a learned skill claims its tests passed; nothing ran them"
        )
        assert version.derived_from, (
            "the run the lesson came from is not recorded, so it cannot be checked"
        )
        assert "tools_used" in version.derived_from

    async def test_the_lesson_names_whose_instructions_it_would_change(self, seeded) -> None:
        """Publication is later and elsewhere; it has to be able to find out."""
        _task_id, agent_id, skill_id = await _learned(seeded)
        version = (
            await seeded.session.execute(
                select(SkillVersion).where(SkillVersion.skill_id == skill_id)
            )
        ).scalar_one()
        assert version.derived_from.get("agent_id") == agent_id


class TestRebindingReplacesRatherThanDuplicates:
    async def test_a_second_lesson_moves_the_pin_rather_than_adding_a_row(self, seeded) -> None:
        """Two rows for one agent and skill makes "which version?" ambiguous.

        `bind_skill` updates the pinned version rather than inserting, and this
        is what that guarantee is for: the agent must run the newest published
        lesson, not whichever row the join happened to find.
        """
        _task_id, agent_id, skill_id = await _learned(seeded)
        await _publish(seeded, skill_id)

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        task = await tasks.create(
            title="price the HVAC package",
            goal="price it again, cheaper this time",
            task_type="execution",
            requester_type="human",
        )
        await tasks.assign(task.id, agent_id)
        await TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_UsingRuntime(),
            run_mode=RunMode.LIVE,
            auto_approve=False,
        ).execute_task(task.id)
        await SkillLearner(seeded.session, seeded.organization_id).learn_from(
            task_id=str(task.id), agent_id=agent_id
        )
        await seeded.session.flush()
        await _publish(seeded, skill_id)

        rows = (
            (
                await seeded.session.execute(
                    select(AgentSkillBinding).where(
                        AgentSkillBinding.organization_id == seeded.organization_id,
                        AgentSkillBinding.agent_id == agent_id,
                        AgentSkillBinding.skill_id == skill_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, (
            f"{len(rows)} bindings for one agent and one skill; 'which version is "
            "this agent running?' no longer has one answer"
        )
        newest = (
            await seeded.session.execute(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill_id)
                .order_by(SkillVersion.version.desc())
                .limit(1)
            )
        ).scalar_one()
        assert rows[0].skill_version_id == str(newest.id), (
            "the pin still points at the first lesson; the agent would keep running "
            "the version it learned first and never see the improvement"
        )
        del tasks
