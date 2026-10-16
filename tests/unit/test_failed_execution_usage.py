"""A failed run keeps the usage it already paid for.

Reproduced incident: a chain-accepted CEO task failed after two durable
`model_usage` rows (input 1382/output 84/reasoning 322, then input 2071/output
64/reasoning 560, both cost 0) and a later model error. The FAILED execution
row said 0/0/0 and the task's spent counters said 0: the known usage survived
only in the per-call ledger and never reached the execution, task or budget
counters. The final unavailable response carries unknown usage and must stay
that way -- nothing here invents it.

What is under test is failure finalization, end to end through
`TaskExecutionService.execute_task` against the migrated test schema as
`ao_app` (never the owner): a runtime records real per-call usage through the
`record_usage` callback and then raises, and the persisted FAILED execution,
the task's cumulative spend and the durable ledger must agree. Offline only:
no provider calls, no touch of existing synthetic rows.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.enums import RunMode, TaskStatus
from ai_orchestrator.domain.errors import ModelUnavailable, PolicyDenied
from ai_orchestrator.models.gateway import ModelResponse, RoutingDecision
from ai_orchestrator.persistence.models import Execution, ModelUsage, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


def _response(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    cost: str = "0",
    finish_reason: str = "",
) -> ModelResponse:
    return ModelResponse(
        usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
        cost_usd=Money(cost),
        model_used=model,
        provider="fake",
        decision=RoutingDecision(
            provider="fake",
            model=model,
            profile="test",
            reason="test",
            routing_reason="primary",
        ),
        finish_reason=finish_reason,
    )


class _FailAfterUsageRuntime(ScriptedRuntime):
    """Records `responses` through `record_usage`, then raises `error`.

    Mirrors what `_RecordingGateway` does on a real run: every provider answer
    -- including rejected completions -- is offered to the recorder as it
    arrives, and the terminal fault itself carries no usage to record.
    """

    name = "fail-after-usage"

    def __init__(self, responses: list[ModelResponse], error: Exception) -> None:
        super().__init__()
        self._responses = responses
        self._error = error

    async def execute(self, task, context, *, record_usage=None, **kwargs):  # type: ignore[no-untyped-def]
        if record_usage is not None:
            for response in self._responses:
                await record_usage(response)
        raise self._error


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _run_failing(seeded, runtime):  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.models import Agent

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Finance Agent",
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
    service = TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=runtime,
        run_mode=RunMode.LIVE,
        auto_approve=False,
    )
    outcome = await service.execute_task(str(task.id))
    return service, str(task.id), outcome


async def _execution_for(seeded, task_id: str) -> Execution:  # type: ignore[no-untyped-def]
    return (
        await seeded.session.execute(
            select(Execution).where(
                Execution.organization_id == seeded.organization_id,
                Execution.task_id == task_id,
            )
        )
    ).scalar_one()


async def _task_for(seeded, task_id: str) -> Task:  # type: ignore[no-untyped-def]
    return (
        await seeded.session.execute(
            select(Task).where(
                Task.organization_id == seeded.organization_id,
                Task.id == task_id,
            )
        )
    ).scalar_one()


async def _ledger_sums(seeded, execution_id: str):  # type: ignore[no-untyped-def]
    row = (
        await seeded.session.execute(
            select(
                func.coalesce(func.sum(ModelUsage.input_tokens), 0),
                func.coalesce(func.sum(ModelUsage.output_tokens), 0),
                func.coalesce(func.sum(ModelUsage.reasoning_tokens), 0),
                func.coalesce(func.sum(ModelUsage.cost_usd), 0),
            ).where(
                ModelUsage.organization_id == seeded.organization_id,
                ModelUsage.execution_id == execution_id,
            )
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2]), float(row[3])


class TestFailureKeepsKnownUsage:
    async def test_model_error_after_recorded_calls_preserves_known_usage(self, seeded) -> None:
        """The incident, replayed: two good turns, then the model goes away."""
        _, task_id, outcome = await _run_failing(
            seeded,
            _FailAfterUsageRuntime(
                [
                    _response(
                        model="dots-1",
                        input_tokens=1382,
                        output_tokens=84,
                        reasoning_tokens=322,
                        cost="0.001",
                    ),
                    _response(
                        model="dots-2",
                        input_tokens=2071,
                        output_tokens=64,
                        reasoning_tokens=560,
                        cost="0.002",
                    ),
                ],
                ModelUnavailable("the model went away mid-run"),
            ),
        )
        assert outcome.status is TaskStatus.FAILED

        execution = await _execution_for(seeded, task_id)
        assert execution.status == "failed"
        assert (execution.input_tokens, execution.output_tokens, execution.reasoning_tokens) == (
            3453,
            148,
            882,
        ), (
            "the two recorded turns sum to 3453/148/882 and the failed "
            f"execution says {execution.input_tokens}/{execution.output_tokens}/"
            f"{execution.reasoning_tokens}"
        )
        assert float(execution.cost_usd) == pytest.approx(0.003)

        task = await _task_for(seeded, task_id)
        assert task.spent_tokens == 3453 + 148 + 882
        assert float(task.spent_usd) == pytest.approx(0.003)

        assert outcome.tokens == 3453 + 148 + 882
        assert float(outcome.cost_usd) == pytest.approx(0.003)

        # The ledger is the source, not a copy: both per-call rows survive with
        # their own values, and the terminal fault added no row of its own.
        assert await _ledger_sums(seeded, str(execution.id)) == (3453, 148, 882, 0.003)
        assert float(execution.cost_usd) == pytest.approx(0.003)

    async def test_policy_denial_counts_rejected_completions_once(self, seeded) -> None:
        """A rejected turn with reported usage still consumed those tokens.

        The gateway records rejected completions individually, so the failure
        path must count the ledger as-is: once, not zero (dropping real spend)
        and not twice (adding the rejection on top of itself).
        """
        _, task_id, outcome = await _run_failing(
            seeded,
            _FailAfterUsageRuntime(
                [
                    _response(
                        model="dots-1",
                        input_tokens=10,
                        output_tokens=20,
                        cost="0.001",
                        finish_reason="error",
                    ),
                    _response(
                        model="dots-2",
                        input_tokens=100,
                        output_tokens=50,
                        reasoning_tokens=5,
                        cost="0.004",
                    ),
                ],
                PolicyDenied("the provider refused the request"),
            ),
        )
        assert outcome.status is TaskStatus.FAILED
        assert outcome.failure_category == "policy_error"

        execution = await _execution_for(seeded, task_id)
        assert (execution.input_tokens, execution.output_tokens, execution.reasoning_tokens) == (
            110,
            70,
            5,
        )
        task = await _task_for(seeded, task_id)
        assert task.spent_tokens == 110 + 70 + 5
        assert float(task.spent_usd) == pytest.approx(0.005)


class TestFailureAccountingInvariants:
    async def test_a_second_finalization_pass_adds_nothing(self, seeded) -> None:
        """Failure finalization converges: repeating it must not spend twice."""
        service, task_id, outcome = await _run_failing(
            seeded,
            _FailAfterUsageRuntime(
                [_response(model="dots-1", input_tokens=200, output_tokens=100, cost="0.001")],
                ModelUnavailable("gone"),
            ),
        )
        assert outcome.status is TaskStatus.FAILED
        before_execution = await _execution_for(seeded, task_id)
        before_task = await _task_for(seeded, task_id)
        before_rows = (
            await seeded.session.execute(
                select(func.count())
                .select_from(ModelUsage)
                .where(
                    ModelUsage.organization_id == seeded.organization_id,
                    ModelUsage.execution_id == str(before_execution.id),
                )
            )
        ).scalar_one()

        task = await service._tasks.get(task_id)
        known_tokens, known_cost = await service._apply_failure_ledger(
            task, str(before_execution.id)
        )

        after_execution = await _execution_for(seeded, task_id)
        after_task = await _task_for(seeded, task_id)
        assert (known_tokens, known_cost) == (300, 0.001)
        assert (
            after_execution.input_tokens,
            after_execution.output_tokens,
            after_execution.reasoning_tokens,
            float(after_execution.cost_usd),
        ) == (
            before_execution.input_tokens,
            before_execution.output_tokens,
            before_execution.reasoning_tokens,
            float(before_execution.cost_usd),
        )
        assert (after_task.spent_tokens, float(after_task.spent_usd)) == (
            before_task.spent_tokens,
            float(before_task.spent_usd),
        )
        after_rows = (
            await seeded.session.execute(
                select(func.count())
                .select_from(ModelUsage)
                .where(
                    ModelUsage.organization_id == seeded.organization_id,
                    ModelUsage.execution_id == str(before_execution.id),
                )
            )
        ).scalar_one()
        assert after_rows == before_rows == 1, "a second finalization must not fabricate usage rows"

    async def test_prior_attempt_spend_is_preserved(self, seeded) -> None:
        """The task row is cumulative: this attempt adds only its own usage."""
        from ai_orchestrator.persistence.models import Agent

        tasks = TaskRepository(seeded.session, seeded.organization_id)
        agent = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Finance Agent",
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
        # A previous attempt already spent 1000 tokens and a cent.
        await tasks.record_spend(str(task.id), tokens=1000, cost_usd=0.01)

        service = TaskExecutionService(
            session=seeded.session,
            organization_id=seeded.organization_id,
            runtime=_FailAfterUsageRuntime(
                [_response(model="dots-1", input_tokens=500, output_tokens=250, cost="0.005")],
                ModelUnavailable("gone again"),
            ),
            run_mode=RunMode.LIVE,
            auto_approve=False,
        )
        outcome = await service.execute_task(str(task.id))
        assert outcome.status is TaskStatus.FAILED

        execution = await _execution_for(seeded, str(task.id))
        assert (execution.input_tokens, execution.output_tokens) == (500, 250)
        task_row = await _task_for(seeded, str(task.id))
        assert task_row.spent_tokens == 1000 + 750
        assert float(task_row.spent_usd) == pytest.approx(0.015)
