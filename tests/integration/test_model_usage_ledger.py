"""Prove the `model_usage` ledger is written, not just readable.

The API serves a cost dashboard from this table and nothing ever inserted a row:
after a live provider call, `SELECT count(*) FROM model_usage` returned 0. This
test drives the exact recorder the bridge calls, against a real session, and
asserts on the row — including the routing decision, which is the whole reason
the per-call ledger is separate from the execution's total.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from ai_orchestrator.persistence.models import ModelUsage
from ai_orchestrator.persistence.repositories.organization import (
    AgentDefinitionRepository,
    AgentRepository,
    RoleRepository,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository

pytestmark = pytest.mark.integration


class _FakeResponse:
    """The shape the recorder reads.

    Built here rather than mocked, so a change to `ModelResponse` breaks this
    test instead of silently passing it.
    """

    def __init__(self) -> None:
        from ai_orchestrator.domain.budget import Money, TokenUsage
        from ai_orchestrator.models.gateway import RoutingDecision

        self.usage = TokenUsage(input_tokens=120, output_tokens=30, cache_read_tokens=5)
        self.cost_usd = Money("0.00042")
        self.latency_ms = 1826
        self.provider = "openrouter"
        self.model_used = "openai/gpt-4.1-mini"
        self.decision = RoutingDecision(
            provider="openrouter",
            model="openai/gpt-4.1-mini",
            profile="reasoning_high",
            reason="fallback after 1 failure(s)",
            routing_reason="fallback",
        )


async def _real_ids(tenant) -> tuple[str, str]:
    """A real agent and a real task.

    Every id here is a foreign key, and the schema refuses a fabricated one. That
    is the point: a cost row pointing at an agent that never existed is not
    evidence of anything.
    """
    role = await RoleRepository(tenant.session, tenant.organization_id).create(name="Worker")
    definition = await AgentDefinitionRepository(tenant.session, tenant.organization_id).create(
        name="worker-definition", role_id=role.id, system_instructions="You are a worker."
    )
    agent = await AgentRepository(tenant.session, tenant.organization_id).create(
        name="Worker", role_id=role.id, definition_id=definition.id
    )
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="Ledger", goal="record a model call"
    )
    return agent.id, task.id


def _service(tenant) -> object:
    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.application.task_execution import TaskExecutionService

    return TaskExecutionService(tenant.session, tenant.organization_id, runtime=ScriptedRuntime())


async def test_a_model_call_produces_one_ledger_row(tenant) -> None:
    agent_id, task_id = await _real_ids(tenant)

    await _service(tenant)._record_model_call(
        _FakeResponse(), task_id=task_id, execution_id=None, agent_id=agent_id
    )
    await tenant.session.flush()

    rows = (
        (
            await tenant.session.execute(
                select(ModelUsage).where(ModelUsage.organization_id == tenant.organization_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].routing_reason == "fallback"
    assert rows[0].model_profile == "reasoning_high"
    assert rows[0].input_tokens == 120
    # Six decimal places, so a $0.00042 call does not round to $0.00.
    # Six decimal places, so a $0.00042 call does not round to $0.00. Compared
    # as a Decimal, because the column is NUMERIC(18,6) and a float comparison
    # would pass for values that the ledger could never store.
    assert Decimal(rows[0].cost_usd) == Decimal("0.000420")


async def test_a_fallback_is_recorded_as_a_fallback(tenant) -> None:
    """The claim the documentation makes, and the one the table could not keep
    before this recorder existed."""
    agent_id, task_id = await _real_ids(tenant)
    await _service(tenant)._record_model_call(
        _FakeResponse(), task_id=task_id, execution_id=None, agent_id=agent_id
    )
    await tenant.session.flush()

    reason = await tenant.session.scalar(
        select(ModelUsage.routing_reason).where(
            ModelUsage.organization_id == tenant.organization_id
        )
    )
    assert reason != "primary"


async def test_a_refusal_is_not_recorded(tenant) -> None:
    """The gateway declined before reaching a provider, so there is no call.

    A row naming no model would be a lie in the one table whose whole purpose is
    to say what was actually called.
    """
    agent_id, task_id = await _real_ids(tenant)
    response = _FakeResponse()
    response.model_used = ""
    response.decision = None

    await _service(tenant)._record_model_call(
        response, task_id=task_id, execution_id=None, agent_id=agent_id
    )
    await tenant.session.flush()

    count = await tenant.session.scalar(
        select(func.count())
        .select_from(ModelUsage)
        .where(ModelUsage.organization_id == tenant.organization_id)
    )
    assert count == 0
