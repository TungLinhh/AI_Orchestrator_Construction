"""Agent registry endpoints.

The registry is the canonical control-plane entity for agents, and these
endpoints are its surface: create, list, inspect, change lifecycle, bind
capabilities, discover.

Lifecycle changes go through the state machine rather than by writing a status
field, so the same illegal jumps are refused whether the caller is a human, a
CLI or a workflow.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from ai_orchestrator.api.deps import ApiContext, get_context, page_params, paginate
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.enums import AutonomyLevel
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.repositories.organization import (
    AgentCapabilityRepository,
    AgentDefinitionRepository,
    AgentRelationshipRepository,
    AgentRepository,
)

router = APIRouter(prefix="/agents", tags=["agents"])


class CreateAgentDefinitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    role_id: str
    system_instructions: str = Field(min_length=1, max_length=100_000)
    model_profile: str = "default"
    allowed_child_roles: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    behavior_config: dict[str, Any] = Field(default_factory=dict)
    memory_policy: dict[str, Any] = Field(default_factory=dict)
    escalation_policy: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = "1"


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    role_id: str
    definition_id: str
    org_unit_id: str | None = None
    parent_agent_id: str | None = None
    description: str = ""
    autonomy_level: str = "l2_parent_review"
    runtime_adapter: str = "pydantic_ai"
    model_profile: str = "default"
    budget_limit_usd: float | None = Field(default=None, ge=0)
    budget_limit_tokens: int | None = Field(default=None, ge=0)
    capabilities: list[str] = Field(default_factory=list)


class UpdateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    model_profile: str | None = Field(default=None, min_length=1, max_length=128)
    autonomy_level: str | None = Field(
        default=None,
        pattern=r"^l[0-4]_(suggest|low_risk_autonomous|parent_review|human_approval|bounded_autonomous)$",
    )
    runtime_adapter: str | None = Field(default=None, min_length=1, max_length=128)
    budget_limit_usd: float | None = Field(default=None, ge=0)
    budget_limit_tokens: int | None = Field(default=None, ge=0)
    capabilities: list[str] | None = None

    @model_validator(mode="after")
    def non_nullable_fields(self) -> UpdateAgentRequest:
        for name in self.model_fields_set - {"budget_limit_usd", "budget_limit_tokens"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class BindSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    skill_version_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class BindToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    max_risk: str = "read_only"
    requires_approval: bool = False
    rate_limit_override: int | None = Field(default=None, ge=1, le=100_000)


class LinkAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str
    relationship_type: str = "peer"
    can_delegate: bool = False
    max_risk: str = "low"
    notes: str = ""


def _agent_dict(agent: Any) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "org_unit_id": agent.org_unit_id,
        "role_id": agent.role_id,
        "definition_id": agent.definition_id,
        "parent_agent_id": agent.parent_agent_id,
        # Business lifecycle and operational status are separate facts. Reporting
        # them as one is what produces "agent ready, task blocked".
        "lifecycle_status": agent.lifecycle_status,
        "runtime_status": agent.runtime_status,
        "health": agent.health,
        "autonomy_level": agent.autonomy_level,
        "runtime_adapter": agent.runtime_adapter,
        "model_profile": agent.model_profile,
        "budget_limit_usd": (
            float(agent.budget_limit_usd) if agent.budget_limit_usd is not None else None
        ),
        "budget_limit_tokens": agent.budget_limit_tokens,
        "active_tasks": agent.active_tasks,
        "queue_depth": agent.queue_depth,
        "capabilities": agent.capabilities,
        "last_heartbeat_at": (
            agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None
        ),
        "definition_version": agent.definition_version,
        "created_at": agent.created_at.isoformat(),
        "retired_at": agent.retired_at.isoformat() if agent.retired_at else None,
    }


@router.post("/definitions", status_code=status.HTTP_201_CREATED)
async def create_definition(
    body: CreateAgentDefinitionRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Create a definition. Activating one that grants elevated capability is a
    separate, approved step — this only authors the blueprint."""
    ctx.require_admin()
    repo = AgentDefinitionRepository(ctx.session, ctx.organization_id)
    definition = await repo.create(
        name=body.name,
        role_id=body.role_id,
        system_instructions=body.system_instructions,
        model_profile=body.model_profile,
        allowed_child_roles=body.allowed_child_roles,
        allowed_task_types=body.allowed_task_types,
        behavior_config=body.behavior_config,
        memory_policy=body.memory_policy,
        escalation_policy=body.escalation_policy,
        prompt_version=body.prompt_version,
        created_by=str(ctx.actor.id),
    )
    bump("agent_definitions_created_total")
    return {
        "id": definition.id,
        "name": definition.name,
        "version": definition.version,
        "role_id": definition.role_id,
        "model_profile": definition.model_profile,
        "prompt_version": definition.prompt_version,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Create an agent instance. Starts in `draft`: activation is separate so
    that provisioning can be reviewed before the agent takes work."""
    ctx.require_admin()
    repo = AgentRepository(ctx.session, ctx.organization_id)
    agent = await repo.create(
        name=body.name,
        role_id=body.role_id,
        definition_id=body.definition_id,
        org_unit_id=body.org_unit_id,
        parent_agent_id=body.parent_agent_id,
        description=body.description,
        autonomy_level=body.autonomy_level,
        runtime_adapter=body.runtime_adapter,
        model_profile=body.model_profile,
        budget_limit_usd=body.budget_limit_usd,
        budget_limit_tokens=body.budget_limit_tokens,
        capabilities=body.capabilities,
    )
    bump("agents_created_total")
    return _agent_dict(agent)


@router.get("")
async def list_agents(
    lifecycle_status: str | None = None,
    org_unit_id: str | None = None,
    page: tuple[int, int] = Depends(page_params),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    repo = AgentRepository(ctx.session, ctx.organization_id)
    agents = await repo.list(lifecycle_status=lifecycle_status, org_unit_id=org_unit_id, limit=500)
    limit, offset = page
    return paginate([_agent_dict(a) for a in agents], limit, offset)


@router.get("/discover")
async def discover_agents(
    capability: list[str] = Query(default_factory=list),
    only_available: bool = True,
    exclude: list[str] = Query(default_factory=list),
    limit: int = Query(20, ge=1, le=100),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Find agents that can take work.

    `exclude` carries the current delegation path, so discovery never proposes an
    agent that would close a cycle. That check belongs here rather than only in
    the delegation authoriser, because discovery is the thing that suggests.
    """
    repo = AgentRepository(ctx.session, ctx.organization_id)
    agents = await repo.discover(
        required_capabilities=capability,
        only_available=only_available,
        exclude=exclude,
        limit=limit,
    )
    bump("agent_discovery_queries_total")
    return {"items": [_agent_dict(a) for a in agents], "count": len(agents)}


@router.get("/{agent_id}")
async def get_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = AgentRepository(ctx.session, ctx.organization_id)
    return _agent_dict(await repo.get(agent_id))


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str, body: UpdateAgentRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Adjust the operational envelope: budgets, capabilities, model profile.

    Lifecycle is not changeable here — it goes through the dedicated endpoints so
    every transition is validated and audited.
    """
    ctx.require_admin()
    repo = AgentRepository(ctx.session, ctx.organization_id)
    agent = await repo.get(agent_id)
    if body.autonomy_level is not None:
        try:
            AutonomyLevel(body.autonomy_level)
        except ValueError as exc:
            raise ValidationError("unknown autonomy level") from exc
    if body.model_profile is not None:
        from ai_orchestrator.application.model_profiles import load_profiles_from_session

        profiles = await load_profiles_from_session(ctx.session, ctx.organization_id)
        if body.model_profile not in profiles.profiles:
            raise ValidationError("unknown model profile", details={"profile": body.model_profile})
    mutable = {
        "description",
        "model_profile",
        "autonomy_level",
        "runtime_adapter",
        "budget_limit_usd",
        "budget_limit_tokens",
        "capabilities",
    }
    for key, value in body.model_dump(exclude_unset=True).items():
        if key not in mutable:
            msg = f"field {key!r} is not mutable through this endpoint"
            raise ValidationError(msg, details={"field": key, "mutable": sorted(mutable)})
        setattr(agent, key, value)
    await ctx.session.flush()
    return _agent_dict(agent)


async def _lifecycle(agent_id: str, ctx: ApiContext, event: Transition) -> dict[str, Any]:
    ctx.require_admin()
    repo = AgentRepository(ctx.session, ctx.organization_id)
    agent = await repo.transition(agent_id, event)
    bump(f"agent_{event.value}_total")
    return _agent_dict(agent)


@router.post("/{agent_id}/activate")
async def activate_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    return await _lifecycle(agent_id, ctx, Transition.ACTIVATE)


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    return await _lifecycle(agent_id, ctx, Transition.PAUSE)


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    return await _lifecycle(agent_id, ctx, Transition.RESUME)


@router.post("/{agent_id}/suspend")
async def suspend_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    return await _lifecycle(agent_id, ctx, Transition.SUSPEND)


@router.post("/{agent_id}/retire")
async def retire_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Retire, never delete. The audit chain has to outlive the entity."""
    return await _lifecycle(agent_id, ctx, Transition.RETIRE)


@router.get("/{agent_id}/capabilities")
async def get_capabilities(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The agent's effective capabilities: bindings plus its declared list.

    The answer to "what can this agent actually do", which is the question an
    operator asks before trusting a delegation.
    """
    agents = AgentRepository(ctx.session, ctx.organization_id)
    agent = await agents.get(agent_id)
    caps = AgentCapabilityRepository(ctx.session, ctx.organization_id)

    skill_ids = await caps.skill_ids_for(agent_id)
    tool_bindings = await caps.tool_bindings_for(agent_id)

    from sqlalchemy import select

    from ai_orchestrator.persistence.models import Skill, Tool

    skills = (
        list(
            (
                await ctx.session.execute(
                    select(Skill.id, Skill.name, Skill.governance_state).where(
                        Skill.id.in_(list(skill_ids))
                    )
                )
            ).all()
        )
        if skill_ids
        else []
    )
    tools = list(
        (
            await ctx.session.execute(
                select(Tool.id, Tool.name, Tool.risk_level, Tool.requires_approval).where(
                    Tool.id.in_([b.tool_id for b in tool_bindings])
                )
            )
        ).all()
    )

    return {
        "agent_id": agent_id,
        "declared": agent.capabilities,
        "skills": [
            {"id": skill_id, "name": name, "governance_state": state}
            for skill_id, name, state in skills
        ],
        "tools": [
            {
                "id": tool_id,
                "name": name,
                "risk_level": risk,
                "requires_approval": requires
                or any(b.requires_approval for b in tool_bindings if b.tool_id == tool_id),
                "binding_max_risk": next(
                    (b.max_risk for b in tool_bindings if b.tool_id == tool_id), None
                ),
            }
            for tool_id, name, risk, requires in tools
        ],
    }


@router.get("/{agent_id}/health")
async def get_health(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Operational health, separate from the business task state.

    `healthy` with zero active tasks is a good sign. An agent that is `active`
    but has not sent a heartbeat is not, and reporting it as healthy would hide a
    dead worker.
    """
    from datetime import UTC, datetime, timedelta

    repo = AgentRepository(ctx.session, ctx.organization_id)
    agent = await repo.get(agent_id)
    stale_after = timedelta(seconds=120)
    now = datetime.now(UTC)
    heartbeat_age = (
        (now - agent.last_heartbeat_at).total_seconds() if agent.last_heartbeat_at else None
    )
    stale = heartbeat_age is None or heartbeat_age > stale_after.total_seconds()
    return {
        "agent_id": agent_id,
        "lifecycle_status": agent.lifecycle_status,
        "runtime_status": agent.runtime_status,
        "health": "unhealthy" if stale and agent.lifecycle_status == "active" else agent.health,
        "heartbeat_age_s": heartbeat_age,
        "heartbeat_stale": stale,
        "active_tasks": agent.active_tasks,
        "queue_depth": agent.queue_depth,
        "estimated_latency_ms": agent.estimated_latency_ms,
        "budget_limit_usd": (
            float(agent.budget_limit_usd) if agent.budget_limit_usd is not None else None
        ),
    }


@router.post("/{agent_id}/skills")
async def bind_skill(
    agent_id: str, body: BindSkillRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Grant a skill. Privileged: a skill carries instructions the agent will follow."""
    ctx.require_admin()
    from ai_orchestrator.persistence.models import SkillVersion

    criteria = [
        (SkillVersion.organization_id == ctx.organization_id)
        | SkillVersion.organization_id.is_(None),
        SkillVersion.skill_id == body.skill_id,
        SkillVersion.is_published.is_(True),
    ]
    if body.skill_version_id:
        criteria.append(SkillVersion.id == body.skill_version_id)
    version = await ctx.session.scalar(
        select(SkillVersion)
        .where(*criteria)
        .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
        .limit(1)
    )
    if version is None:
        raise ValidationError("Bind requires a published version of this skill")
    repo = AgentCapabilityRepository(ctx.session, ctx.organization_id)
    binding = await repo.bind_skill(
        agent_id=agent_id,
        skill_id=body.skill_id,
        skill_version_id=version.id,
        constraints=body.constraints,
        granted_by=str(ctx.actor.id),
    )
    bump("skill_bindings_created_total")
    return {"agent_id": agent_id, "skill_id": binding.skill_id, "is_enabled": binding.is_enabled}


@router.post("/{agent_id}/tools")
async def bind_tool(
    agent_id: str, body: BindToolRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Grant a tool. Privileged.

    The binding may carry a *narrower* risk ceiling than the tool's own. It may
    never carry a wider one: a grant is a limit, not an override.
    """
    ctx.require_admin()
    from sqlalchemy import select

    from ai_orchestrator.domain.enums import ToolRisk, tool_risk_exceeds
    from ai_orchestrator.persistence.models import Tool

    tool = (
        await ctx.session.execute(
            select(Tool).where(Tool.id == body.tool_id, Tool.organization_id == ctx.organization_id)
        )
    ).scalar_one_or_none()
    if tool is None:
        msg = f"tool not found: {body.tool_id}"
        raise NotFoundError(msg, resource_type="tool", resource_id=body.tool_id)

    if tool_risk_exceeds(ToolRisk(body.max_risk), ToolRisk(tool.risk_level)):
        msg = (
            f"binding risk {body.max_risk} is wider than the tool's own risk "
            f"{tool.risk_level}; a grant may only narrow"
        )
        raise ValidationError(
            msg, details={"binding_max_risk": body.max_risk, "tool_risk": tool.risk_level}
        )

    repo = AgentCapabilityRepository(ctx.session, ctx.organization_id)
    binding = await repo.bind_tool(
        agent_id=agent_id,
        tool_id=body.tool_id,
        max_risk=body.max_risk,
        requires_approval=body.requires_approval,
        rate_limit_override=body.rate_limit_override,
        granted_by=str(ctx.actor.id),
    )
    bump("tool_bindings_created_total")
    return {"agent_id": agent_id, "tool_id": binding.tool_id, "max_risk": binding.max_risk}


@router.delete("/{agent_id}/tools/{tool_id}")
async def revoke_tool(
    agent_id: str, tool_id: str, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Disable rather than delete: an operator needs to see that an agent once
    had a destructive tool and when it was taken away."""
    ctx.require_admin()
    repo = AgentCapabilityRepository(ctx.session, ctx.organization_id)
    await repo.revoke_tool(agent_id, tool_id)
    bump("tool_bindings_revoked_total")
    return {"agent_id": agent_id, "tool_id": tool_id, "is_enabled": False}


@router.post("/{agent_id}/relationships")
async def link_agent(
    agent_id: str, body: LinkAgentRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Declare a peer relationship. Default-deny: no edge means no peer delegation."""
    ctx.require_admin()
    repo = AgentRelationshipRepository(ctx.session, ctx.organization_id)
    rel = await repo.link(
        source_agent_id=agent_id,
        target_agent_id=body.target_agent_id,
        relationship_type=body.relationship_type,
        can_delegate=body.can_delegate,
        max_risk=body.max_risk,
        notes=body.notes,
    )
    return {
        "id": rel.id,
        "source_agent_id": rel.source_agent_id,
        "target_agent_id": rel.target_agent_id,
        "can_delegate": rel.can_delegate,
    }


@router.get("/{agent_id}/children")
async def list_children(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = AgentRepository(ctx.session, ctx.organization_id)
    children = await repo.children(agent_id)
    return {"items": [_agent_dict(a) for a in children], "count": len(children)}


__all__ = ["router"]
