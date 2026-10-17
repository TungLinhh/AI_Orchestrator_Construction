"""Explicit, optimistic prompt revision for an existing agent.

Seeding never overwrites tenant customizations. An operator reviews the current
prompt and supplies its hash; stale plans fail rather than replacing newer work.
Definitions remain immutable history and only the selected agent changes pin.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError, ValidationError
from ai_orchestrator.domain.ids import AgentDefinitionId
from ai_orchestrator.persistence.models import Agent, AgentDefinition, Execution
from ai_orchestrator.persistence.repositories.organization import AgentDefinitionRepository


def prompt_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


async def revise_agent_prompt(
    session: AsyncSession,
    organization_id: str,
    *,
    agent_id: str,
    instructions: str,
    revision: str,
    expected_hash: str,
    actor: Actor,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or apply one reviewed revision in the caller's tenant transaction."""
    if not instructions.strip() or len(instructions) > 100_000:
        raise ValidationError("Prompt must contain 1-100000 characters")
    if not revision.strip() or len(revision) > 128:
        raise ValidationError("Prompt revision must contain 1-128 characters")
    # Serialize versions across agents sharing the same definition name. Locking
    # just the agent would let two operators allocate the same unique version.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"prompt-revision:{organization_id}"},
    )
    agent = (
        await session.execute(
            select(Agent)
            .where(Agent.organization_id == organization_id, Agent.id == agent_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    old = await AgentDefinitionRepository(session, organization_id).get(agent.definition_id)
    before = prompt_hash(old.system_instructions)
    after = prompt_hash(instructions)
    receipt: dict[str, Any] = {
        "organization_id": organization_id,
        "agent_id": agent_id,
        "old_definition_id": old.id,
        "old_version": old.version,
        "before_sha256": before,
        "after_sha256": after,
        "prompt_version": revision,
        "applied": False,
    }
    if before == after and old.prompt_version == revision:
        return {**receipt, "status": "unchanged", "definition_id": old.id}
    if before != expected_hash:
        raise PreconditionError("Prompt changed since review; preserve it and review again")
    active = await session.scalar(
        select(func.count())
        .select_from(Execution)
        .where(
            Execution.organization_id == organization_id,
            Execution.agent_id == agent_id,
            Execution.status.in_(["pending", "running", "started"]),
        )
    )
    if active:
        raise PreconditionError("Agent has active executions; retry after they settle")
    if not apply:
        return {**receipt, "status": "planned"}
    version = (
        await session.scalar(
            select(func.max(AgentDefinition.version)).where(
                AgentDefinition.organization_id == organization_id,
                AgentDefinition.name == old.name,
            )
        )
        or 0
    ) + 1
    new = AgentDefinition(
        id=str(AgentDefinitionId.create()),
        organization_id=organization_id,
        version=version,
        is_active=old.is_active,
        name=old.name,
        role_id=old.role_id,
        system_instructions=instructions,
        model_profile=old.model_profile,
        allowed_child_roles=deepcopy(old.allowed_child_roles),
        allowed_task_types=deepcopy(old.allowed_task_types),
        behavior_config=deepcopy(old.behavior_config),
        memory_policy=deepcopy(old.memory_policy),
        escalation_policy=deepcopy(old.escalation_policy),
        prompt_version=revision,
        created_by=str(actor.id) if actor.id else None,
    )
    session.add(new)
    # ORM mappings have no relationship edge to order this cyclic FK automatically.
    await session.flush([new])
    agent.definition_id = new.id
    agent.definition_version = version
    await session.flush()
    receipt.update(applied=True, status="applied", definition_id=new.id, version=version)
    await AuditService(session, organization_id).record(
        actor=actor,
        action="agent.prompt.revise",
        resource_type="agent",
        resource_id=agent_id,
        context=receipt,
    )
    return receipt
