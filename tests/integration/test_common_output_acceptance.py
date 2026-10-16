"""Provider completion and accepted departmental output are distinct outcomes."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.models import Agent, Execution, ModelUsage
from ai_orchestrator.persistence.repositories.task import TaskRepository
from tests.integration.test_a_published_skill_reaches_its_agent import seeded as seeded

pytestmark = pytest.mark.integration


class CompletedRuntime:
    name = "synthetic-acceptance-probe"

    def __init__(self, output):
        self.output = output

    async def execute(self, task, context, *, record_usage=None, **kwargs):
        usage = TokenUsage(input_tokens=20, output_tokens=5)
        if record_usage:
            await record_usage(
                ModelResponse(
                    text="synthetic output",
                    model_used="synthetic",
                    provider="fake",
                    usage=usage,
                    cost_usd=Money("0.0001"),
                )
            )
        return AgentResult(
            task_id=task.task_id,
            execution_id=task.execution_id,
            status=AgentResultStatus.COMPLETED,
            summary="Synthetic provider completed.",
            output=self.output,
            usage=usage,
            cost_usd=Money("0.0001"),
        )


@pytest.mark.parametrize("value,accepted", [(None, False), ("3", False), (3, True)])
async def test_common_finish_checks_values_and_preserves_usage(seeded, value, accepted):
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id, Agent.name == "Finance Agent"
            )
        )
    ).scalar_one()
    repository = TaskRepository(seeded.session, seeded.organization_id)
    task = await repository.create(
        title="Synthetic departmental acceptance",
        goal="Return a count.",
        task_type="execution",
        requester_type="human",
        owner_agent_id=agent.id,
        expected_output_schema={
            "required": ["count"],
            "properties": {"count": {"type": "integer"}},
        },
    )
    outcome = await TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=CompletedRuntime({"count": value}),
        auto_approve=False,
    ).execute_task(task.id)
    assert outcome.status == ("completed" if accepted else "failed")
    if not accepted:
        assert outcome.failure_category == "output_contract_unmet"
    execution = await seeded.session.get(Execution, outcome.execution_id)
    assert execution.status == outcome.status
    assert execution.input_tokens == 20
    assert execution.output_tokens == 5
    assert float(execution.cost_usd) == 0.0001
    if not accepted:
        assert execution.error_category == "output_contract_unmet"
    rows = (
        (
            await seeded.session.execute(
                select(ModelUsage).where(ModelUsage.execution_id == execution.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "ok"  # provider returned; artifact acceptance is separate
    assert rows[0].input_tokens == 20
