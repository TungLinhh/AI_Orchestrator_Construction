"""A real persisted delegation for stream projection and browser checks."""

from sqlalchemy import select

from ai_orchestrator.domain.delegation import DelegationLimits, DelegationPath
from ai_orchestrator.domain.ids import AgentId, TaskId
from ai_orchestrator.persistence.models import Agent
from ai_orchestrator.persistence.repositories.task import DelegationRepository, TaskRepository


async def create_delegation_event(tenant):
    agents = {
        a.name: a
        for a in await tenant.session.scalars(
            select(Agent).where(Agent.organization_id == tenant.organization_id)
        )
    }
    chief, finance = agents["Executive Agent"], agents["Finance Agent"]
    tasks = TaskRepository(tenant.session, tenant.organization_id)
    parent = await tasks.create(
        title="Review delegation", goal="Review source budget", owner_agent_id=chief.id
    )
    child = await tasks.create(
        title="Finance review",
        goal="Prepare budget review",
        owner_agent_id=finance.id,
        parent_task_id=parent.id,
    )
    limits = DelegationLimits.platform_default()
    await DelegationRepository(tenant.session, tenant.organization_id).record(
        parent_task_id=parent.id,
        source_agent_id=chief.id,
        target_agent_id=finance.id,
        objective="Prepare budget review",
        child_task_id=child.id,
        path=DelegationPath.root(AgentId(chief.id), TaskId(parent.id)),
        platform_limits=limits,
        parent_limits=limits,
    )
    await tenant.commit()
