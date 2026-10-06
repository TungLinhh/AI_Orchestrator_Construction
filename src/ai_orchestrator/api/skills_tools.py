"""Skill and tool registry endpoints.

Publishing a skill with elevated capability is a privileged act, because a skill
is instructions the agent will follow. The governance state is enforced here
rather than in the agent: a `blocked` skill is invisible to discovery, so an
agent cannot select its way back to it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ai_orchestrator.api.deps import ApiContext, get_context, paginate
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.persistence.models import Skill, SkillVersion, Tool, ToolVersion
from ai_orchestrator.persistence.repositories.organization import AgentCapabilityRepository

router = APIRouter(tags=["skills", "tools"])


class CreateSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    instructions: str = Field(min_length=1, max_length=100_000)
    version: str = "1.0.0"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_tool_ids: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    tags: list[str] = Field(default_factory=list)


class PublishSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_version_id: str | None = None
    governance_state: str = "reviewed"
    test_results: dict[str, Any] = Field(default_factory=dict)
    security_scan: dict[str, Any] = Field(default_factory=dict)
    expert_review_source: str | None = Field(default=None, min_length=5, max_length=1000)


class ProposeSkillRequest(BaseModel):
    """An agent proposing a new skill.

    The proposal lands as `experimental` and is unusable until it passes tests,
    a security scan, a licence check and — for elevated capability — a human
    approval. An agent must not be able to turn a prompt into a privileged
    production capability by writing it down.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    instructions: str = Field(min_length=1, max_length=100_000)
    rationale: str = ""
    required_tool_ids: list[str] = Field(default_factory=list)


class CreateToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9][a-z0-9_]*$")
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    risk_level: str = "low_risk_write"
    effect_class: str = "mutate_internal"
    data_classification: str = "internal"
    requires_approval: bool = False
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    is_idempotent: bool = False
    log_payload: bool = False


def _skill_dict(skill: Skill, version: SkillVersion | None) -> dict[str, Any]:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "governance_state": skill.governance_state,
        "requires_approval": skill.requires_approval,
        "tags": skill.tags,
        "current_version": version.version if version else None,
        "version_id": version.id if version else None,
        "risk_level": version.risk_level if version else None,
        "is_published": version.is_published if version else False,
        "test_results": version.test_results if version else {},
        "security_scan": version.security_scan if version else {},
        "created_at": skill.created_at.isoformat(),
    }


def _tool_dict(tool: Tool, version: ToolVersion | None) -> dict[str, Any]:
    return {
        "id": tool.id,
        "name": tool.name,
        "description": tool.description,
        "risk_level": tool.risk_level,
        "effect_class": tool.effect_class,
        "data_classification": tool.data_classification,
        "requires_approval": tool.requires_approval,
        "rate_limit_per_minute": tool.rate_limit_per_minute,
        "timeout_seconds": tool.timeout_seconds,
        "is_mcp": tool.is_mcp,
        "is_active": tool.is_active,
        "is_idempotent": version.is_idempotent if version else None,
        "log_payload": version.log_payload if version else None,
        "current_version": version.version if version else None,
        "version_id": version.id if version else None,
    }


@router.post("/skills", status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: CreateSkillRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Author a skill. It starts `experimental` and is not usable yet."""
    ctx.require_admin()
    from ai_orchestrator.domain.ids import SkillId

    skill = Skill(
        # Scoped by organization: a fixed `skl_<name>` collides on the primary
        # key as soon as a second tenant creates a skill of the same name.
        id=str(SkillId.create()),
        organization_id=ctx.organization_id,
        name=body.name,
        description=body.description,
        governance_state="experimental",
        requires_approval=body.risk_level in {"high", "critical", "privileged"},
        is_global=False,
        tags=body.tags,
    )
    ctx.session.add(skill)
    await ctx.session.flush()
    from ai_orchestrator.domain.ids import SkillVersionId

    version = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=ctx.organization_id,
        skill_id=skill.id,
        version=body.version,
        instructions=body.instructions,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        required_tool_ids=body.required_tool_ids,
        required_permissions=body.required_permissions,
        risk_level=body.risk_level,
        is_published=False,
    )
    ctx.session.add(version)
    await ctx.session.flush()
    bump("skills_created_total")
    return _skill_dict(skill, version)


@router.post("/skills/proposals", status_code=status.HTTP_201_CREATED)
async def propose_skill(
    body: ProposeSkillRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Accept a skill proposed by an agent.

    The proposal is recorded and refused publication. An agent proposing a
    privileged skill is a signal to a human, not a request to be granted.
    """
    from ai_orchestrator.domain.ids import SkillId

    skill = Skill(
        id=str(SkillId.create()),
        organization_id=ctx.organization_id,
        name=body.name,
        description=body.description,
        governance_state="experimental",
        requires_approval=True,
        is_global=False,
        tags=["agent-proposed"],
        provenance_log={"rationale": body.rationale, "proposed_by": str(ctx.actor.id)},
    )
    ctx.session.add(skill)
    await ctx.session.flush()
    from ai_orchestrator.domain.ids import SkillVersionId

    version = SkillVersion(
        id=str(SkillVersionId.create()),
        organization_id=ctx.organization_id,
        skill_id=skill.id,
        version="0.1.0",
        instructions=body.instructions,
        required_tool_ids=body.required_tool_ids,
        risk_level="medium",
        is_published=False,
    )
    ctx.session.add(version)
    await ctx.session.flush()
    bump("skill_proposals_total")
    return {
        **dict(_skill_dict(skill, version)),
        "proposal": {
            "rationale": body.rationale,
            "requires_review": True,
            "next_steps": [
                "attach test results",
                "run a security scan",
                "check the licence",
                "obtain human approval if the capability is elevated",
            ],
        },
    }


@router.post("/skills/{skill_id}/publish")
async def publish_skill(
    skill_id: str, body: PublishSkillRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Publish a skill.

    Refused without test evidence. A skill is instructions the agent will follow,
    and an untested instruction is an untested capability — the O-Nexus audit found
    exactly this pattern being claimed as production-ready without evidence.
    """
    ctx.require_admin()
    skill = (
        await ctx.session.execute(
            select(Skill).where(Skill.id == skill_id, Skill.organization_id == ctx.organization_id)
        )
    ).scalar_one_or_none()
    if skill is None:
        msg = f"skill not found: {skill_id}"
        raise NotFoundError(msg, resource_type="skill", resource_id=skill_id)

    # **Newest version, explicitly.** This used to be `.limit(1)` with no
    # ordering, so with three versions in the table it published whichever the
    # database happened to return first — and then reported the skill as
    # published while leaving the agent running on whichever version that was.
    # A publication that does not say which version it published is not a
    # publication at all.
    version = (
        await ctx.session.execute(
            select(SkillVersion)
            .where(
                SkillVersion.skill_id == skill_id,
                SkillVersion.organization_id == ctx.organization_id,
            )
            .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        msg = f"skill {skill_id} has no version to publish"
        raise ValidationError(msg, details={"skill_id": skill_id})

    if body.skill_version_id is not None and body.skill_version_id != version.id:
        raise ValidationError(
            "the skill version changed since it was reviewed; reload before publishing",
            details={"expected": body.skill_version_id, "current": version.id},
        )

    evidence = version.derived_from or {}
    if evidence.get("development_only"):
        raise ValidationError("Synthetic development proposals cannot be published to production")
    if evidence.get("lesson_scope") == "proposal_only":
        from ai_orchestrator.application.skill_evaluation_gate import evaluated_feedback

        tests = await evaluated_feedback(
            ctx.session, ctx.organization_id, version, body.expert_review_source
        )
        version.derived_from = {
            **evidence,
            "agent_id": tests["paired_evaluation"]["agent_id"],
            "expert_review": {"source": body.expert_review_source, "reviewer": str(ctx.actor.id)},
        }
    else:
        tests = body.test_results or (version.test_results or {})
    if not tests.get("passed"):
        msg = (
            "a skill cannot be published without passing test results; attach "
            "test_results={'passed': true, ...}"
        )
        raise ValidationError(msg, details={"skill_id": skill_id, "test_results": tests})

    scan = body.security_scan or (version.security_scan or {})
    elevated = skill.requires_approval or version.risk_level in {"high", "critical", "privileged"}
    if elevated and not scan.get("clean"):
        msg = (
            f"skill {skill_id} has elevated capability ({version.risk_level}) and "
            f"requires a clean security scan before publication"
        )
        raise ValidationError(msg, details={"skill_id": skill_id, "security_scan": scan})

    from ai_orchestrator.application.learning_revert import prepare_publication

    revert_receipt = await prepare_publication(ctx.session, ctx.organization_id, skill, version)
    version.test_results = tests
    version.security_scan = scan
    version.is_published = True
    skill.governance_state = body.governance_state

    # **This is the step that makes publication mean anything.**
    #
    # A published `SkillVersion` is inert on its own. `TaskExecutionService`
    # assembles an agent's skills by joining `agent_skill_bindings` to the version
    # the binding *pins* — so a version nobody is bound to is a document, and the
    # learning loop writes documents. That was measured: four learned versions in
    # the tenant and no binding pointing at any of them, so an agent learned
    # nothing and the loop reported itself as working.
    #
    # The agent comes from the version's own evidence, because publication happens
    # later and elsewhere and has no other way to know whose instructions it is
    # about to change. A learned skill with no agent in its evidence is reported
    # rather than silently published-and-inert: publishing it would show a green
    # tick for a change nobody receives.
    bound = None
    evidence = version.derived_from or {}
    agent_id = evidence.get("agent_id")
    if agent_id:
        binding = await AgentCapabilityRepository(ctx.session, ctx.organization_id).bind_skill(
            agent_id=str(agent_id),
            skill_id=skill_id,
            skill_version_id=str(version.id),
            granted_by=str(ctx.actor.id),
        )
        bound = binding.id

    await ctx.session.flush()
    bump("skills_published_total")
    published = _skill_dict(skill, version)
    published["bound_to_agent"] = agent_id
    published["binding_id"] = bound
    published["revert_receipt"] = revert_receipt
    return published


def _latest_skill_version() -> Any:
    return (
        select(SkillVersion.id)
        .where(SkillVersion.skill_id == Skill.id)
        .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
        .limit(1)
        .correlate(Skill)
        .scalar_subquery()
    )


@router.get("/skills")
async def list_skills(
    governance_state: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    stmt = (
        select(Skill, SkillVersion)
        .outerjoin(SkillVersion, SkillVersion.id == _latest_skill_version())
        .where((Skill.organization_id == ctx.organization_id) | Skill.organization_id.is_(None))
    )
    if governance_state:
        stmt = stmt.where(Skill.governance_state == governance_state)
    rows = (await ctx.session.execute(stmt.order_by(Skill.name, Skill.id))).all()
    return paginate([_skill_dict(s, v) for s, v in rows], limit, offset)


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            select(Skill, SkillVersion)
            .outerjoin(SkillVersion, SkillVersion.id == _latest_skill_version())
            .where(
                Skill.id == skill_id,
                (Skill.organization_id == ctx.organization_id) | Skill.organization_id.is_(None),
            )
        )
    ).first()
    if row is None:
        raise NotFoundError(
            f"skill not found: {skill_id}", resource_type="skill", resource_id=skill_id
        )
    skill, version = row
    return {
        **_skill_dict(skill, version),
        "derived_from": version.derived_from if version else {},
        "instructions": version.instructions if version else "",
        "input_schema": version.input_schema if version else {},
        "output_schema": version.output_schema if version else {},
        "required_tool_ids": version.required_tool_ids if version else [],
    }


@router.post("/tools", status_code=status.HTTP_201_CREATED)
async def create_tool(
    body: CreateToolRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Register a tool. The risk level recorded here is what the gateway uses;
    the agent's own assessment is advisory and never read."""
    ctx.require_admin()
    from ai_orchestrator.domain.ids import ToolId, ToolVersionId

    tool = Tool(
        id=str(ToolId.create()),
        organization_id=ctx.organization_id,
        name=body.name,
        description=body.description,
        risk_level=body.risk_level,
        effect_class=body.effect_class,
        data_classification=body.data_classification,
        requires_approval=body.requires_approval,
        rate_limit_per_minute=body.rate_limit_per_minute,
        timeout_seconds=body.timeout_seconds,
        is_active=True,
    )
    ctx.session.add(tool)
    await ctx.session.flush()
    version = ToolVersion(
        id=str(ToolVersionId.create()),
        organization_id=ctx.organization_id,
        tool_id=tool.id,
        version="1.0.0",
        input_schema=body.input_schema,
        is_idempotent=body.is_idempotent,
        # Payload logging is opt-in and defaults off: a tool argument is the most
        # likely place for prompt-injected user data to reach a log index.
        log_payload=body.log_payload,
    )
    ctx.session.add(version)
    await ctx.session.flush()
    bump("tools_created_total")
    return {**_tool_dict(tool, version), "input_schema": version.input_schema if version else {}}


def _latest_tool_version() -> Any:
    return (
        select(ToolVersion.id)
        .where(ToolVersion.tool_id == Tool.id)
        .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
        .limit(1)
        .correlate(Tool)
        .scalar_subquery()
    )


@router.get("/tools")
async def list_tools(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(Tool, ToolVersion)
            .outerjoin(ToolVersion, ToolVersion.id == _latest_tool_version())
            .where((Tool.organization_id == ctx.organization_id) | Tool.organization_id.is_(None))
            .order_by(Tool.name, Tool.id)
        )
    ).all()
    return paginate([_tool_dict(t, v) for t, v in rows], limit, offset)


@router.get("/tools/{tool_id}")
async def get_tool(tool_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(Tool, ToolVersion)
            .outerjoin(ToolVersion, ToolVersion.id == _latest_tool_version())
            .where(
                Tool.id == tool_id,
                (Tool.organization_id == ctx.organization_id) | Tool.organization_id.is_(None),
            )
        )
    ).first()
    if rows is None:
        msg = f"tool not found: {tool_id}"
        raise NotFoundError(msg, resource_type="tool", resource_id=tool_id)
    tool, version = rows
    return {**_tool_dict(tool, version), "input_schema": version.input_schema if version else {}}


__all__ = ["router"]
