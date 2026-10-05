"""Publish through the endpoint and falsify the lesson through an actual execution."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ai_orchestrator.application.learning_revert import revert_falsified_skills
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.domain.errors import ConflictError
from ai_orchestrator.domain.ids import SkillVersionId
from ai_orchestrator.persistence.models import AgentSkillBinding, AuditLog, Execution, SkillVersion
from ai_orchestrator.persistence.repositories.task import TaskRepository
from tests.integration.test_a_published_skill_reaches_its_agent import _learned, _publish
from tests.integration.test_a_published_skill_reaches_its_agent import seeded as seeded

pytestmark = pytest.mark.integration


class _BadContractRuntime:
    name = "bad-contract"

    async def execute(self, task, context, **kwargs):
        assert any(skill.instructions for skill in context.authorized_skills)
        return AgentResult(
            task_id=str(task.task_id),
            execution_id=str(task.execution_id),
            status=AgentResultStatus.COMPLETED,
            summary="The promised report is missing from this execution.",
            output={"other": "No reconciliation was produced in the promised field."},
        )


async def _binding(seeded, skill_id):
    return (
        await seeded.session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.organization_id == seeded.organization_id,
                AgentSkillBinding.skill_id == skill_id,
            )
        )
    ).scalar_one()


async def _falsify(seeded, agent_id):
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="Later reconciliation",
        goal="Produce the next reconciliation under the published lesson",
        task_type="execution",
        requester_type="human",
        expected_output_schema={"required": ["report"]},
    )
    await tasks.assign(str(task.id), agent_id)
    outcome = await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=_BadContractRuntime(),
        auto_approve=False,
    ).execute_task(str(task.id))
    assert outcome.status == "failed"
    assert outcome.failure_category == "output_contract_unmet"
    return task, outcome


async def test_first_publication_can_be_removed_without_a_human(seeded):
    _, agent, skill_id = await _learned(seeded)
    publication = await _publish(seeded, skill_id)
    assert publication["revert_receipt"]["falsifier_kind"] == "output_contract"
    binding = await _binding(seeded, skill_id)
    version_id = str(binding.skill_version_id)
    task, outcome = await _falsify(seeded, agent)
    assert not binding.is_enabled
    assert binding.skill_version_id is None
    version = await seeded.session.get(SkillVersion, version_id)
    assert not version.is_published
    assert version.derived_from["reverted_by_task"] == str(task.id)
    execution = await seeded.session.get(Execution, outcome.execution_id)
    assert execution.skill_versions[skill_id] == version.version
    assert (
        await revert_falsified_skills(
            seeded.session, seeded.organization_id, task, outcome.execution_id
        )
        == []
    )
    audit = (
        await seeded.session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == seeded.organization_id,
                AuditLog.action == "skill.automatically_reverted",
                AuditLog.resource_id == version_id,
            )
        )
    ).scalar_one()
    assert audit.task_id == str(task.id)
    with pytest.raises(ConflictError, match="falsified"):
        await _publish(seeded, skill_id)


async def test_reversion_restores_the_previous_pin_and_constraints(seeded):
    _, agent, skill_id = await _learned(seeded)
    await _publish(seeded, skill_id)
    binding = await _binding(seeded, skill_id)
    previous_id = str(binding.skill_version_id)
    binding.constraints = {"purpose": "approved baseline"}
    previous = await seeded.session.get(SkillVersion, previous_id)
    candidate = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=seeded.organization_id,
        skill_id=skill_id,
        version="2",
        instructions="Use the revised reconciliation sequence.",
        derived_from={"agent_id": agent, "task_title": "reconciliation"},
    )
    seeded.session.add(candidate)
    await seeded.session.flush()
    await _publish(seeded, skill_id)
    assert binding.skill_version_id == candidate.id
    await _falsify(seeded, agent)
    assert binding.skill_version_id == previous_id
    assert binding.is_enabled
    assert binding.constraints == {"purpose": "approved baseline"}
    assert previous.is_published


async def test_a_late_result_cannot_revert_a_newer_publication(seeded):
    _, agent, skill_id = await _learned(seeded)
    await _publish(seeded, skill_id)
    binding = await _binding(seeded, skill_id)
    original = await seeded.session.get(SkillVersion, str(binding.skill_version_id))

    class GoodRuntime(_BadContractRuntime):
        async def execute(self, task, context, **kwargs):
            result = await super().execute(task, context, **kwargs)
            return result.model_copy(
                update={
                    "output": {"report": "Both ledgers reconcile without any unexplained balance."}
                }
            )

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="Result arriving after publication",
        goal="Reconcile the ledgers",
        task_type="execution",
        requester_type="human",
        expected_output_schema={"required": ["report"]},
    )
    await tasks.assign(str(task.id), agent)
    result = await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=GoodRuntime(),
        auto_approve=False,
    ).execute_task(str(task.id))
    assert result.status == "completed"
    candidate = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=seeded.organization_id,
        skill_id=skill_id,
        version="2",
        instructions="Use a corrected reconciliation sequence.",
        derived_from={"agent_id": agent},
    )
    seeded.session.add(candidate)
    await seeded.session.flush()
    await _publish(seeded, skill_id)
    task.output = {"other": "The old result did not contain its promised report."}
    reverted = await revert_falsified_skills(
        seeded.session,
        seeded.organization_id,
        task,
        result.execution_id,
    )
    assert reverted == []
    assert binding.skill_version_id == candidate.id
    assert binding.is_enabled and candidate.is_published
    assert original.is_published
    assert "reverted_by_task" not in original.derived_from


async def test_a_provider_crash_does_not_falsify_the_procedure(seeded):
    _, agent, skill_id = await _learned(seeded)
    await _publish(seeded, skill_id)
    binding = await _binding(seeded, skill_id)
    version = await seeded.session.get(SkillVersion, str(binding.skill_version_id))

    class CrashRuntime:
        name = "crashed-provider"

        async def execute(self, *args, **kwargs):
            raise RuntimeError("Provider connection was lost")

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="Provider failure",
        goal="Reconcile the ledgers",
        task_type="execution",
        requester_type="human",
        expected_output_schema={"required": ["report"]},
    )
    await tasks.assign(str(task.id), agent)
    outcome = await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=CrashRuntime(),
        auto_approve=False,
    ).execute_task(str(task.id))
    assert outcome.status == "failed"
    assert outcome.failure_category != "output_contract_unmet"
    assert binding.skill_version_id == version.id
    assert binding.is_enabled and version.is_published
    assert "reverted_by_task" not in version.derived_from
