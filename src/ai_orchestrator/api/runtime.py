"""Runtime endpoints: model profiles, tool invocation and memory.

These exist so an operator can inspect and drive the runtime without a shell.
Tool invocation goes through the same gateway an agent uses, so a human
diagnosing a problem cannot take a path an agent is denied.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ai_orchestrator.api.deps import ApiContext, get_context, paginate, require_body_tenant
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.enums import (
    AutonomyLevel,
    DataClassification,
    RunMode,
)
from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.domain.ids import ExecutionId, OrganizationId
from ai_orchestrator.memory import HashEmbedder, MemoryService
from ai_orchestrator.persistence.models import ModelUsage

router = APIRouter(tags=["runtime"])


class InvokeToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    #: Defaults to simulation. A human invoking a side-effecting tool has to say
    #: `run_mode: live` explicitly, so an exploratory curl cannot send an email.
    run_mode: str = "simulation"


class ModelCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    prompt: str = Field(min_length=1, max_length=100_000)
    system_instructions: str = ""
    max_output_tokens: int = Field(default=500, ge=1, le=32_000)
    data_classification: str = "internal"
    organization_id: str | None = None


class WriteMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=1_000_000)
    tier: str = "working"
    kind: str = "note"
    title: str = ""
    classification: str = "internal"
    source_type: str = "document"
    source_ref: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    organization_id: str | None = None


class SearchMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=10_000)
    max_results: int = Field(default=8, ge=1, le=50)
    max_classification: str = "internal"
    organization_id: str | None = None


@router.get("/model-profiles")
async def list_model_profiles(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The profile catalogue.

    Profiles name a capability requirement, not a model. Reading this list is how
    an operator answers "can I change the provider without editing agents?" —
    yes, because agents never name a provider.
    """
    from ai_orchestrator.application.model_profiles import load_profiles_from_session

    load = await load_profiles_from_session(ctx.session, ctx.organization_id)
    return {
        **paginate(
            [
                {
                    "name": p.name,
                    "description": p.description,
                    "providers": [
                        {
                            "provider": c.provider,
                            "model": c.model,
                            "weight": c.weight,
                            "max_input_tokens": c.max_input_tokens,
                            "supports_tools": c.supports_tools,
                            "supports_structured_output": c.supports_structured_output,
                            "max_classification": c.max_classification.value,
                        }
                        for c in p.candidates
                    ],
                    "fallback_profile": p.fallback_profile,
                    "max_classification": p.max_classification.value,
                    "requires_tools": p.requires_tools,
                    "requires_structured_output": p.requires_structured_output,
                    "is_active": True,
                }
                for p in load.profiles.values()
            ],
            100,
            0,
        ),
        "warnings": load.warnings,
    }


@router.post("/model/call")
async def call_model(
    body: ModelCallRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Call a model through the gateway.

    Exposed so an operator can verify a credential and see the routing decision
    without writing a script. The classification travels with the request, so the
    privacy filter applies here exactly as it does for an agent.
    """
    from ai_orchestrator.application.model_profiles import build_tenant_gateway
    from ai_orchestrator.models.gateway import ModelRequest

    require_body_tenant(body.model_dump(), ctx)
    gateway = await build_tenant_gateway(ctx.organization_id, session=ctx.session)

    request = ModelRequest(
        profile=body.profile,
        system_instructions=body.system_instructions,
        prompt=body.prompt,
        max_output_tokens=body.max_output_tokens,
        data_classification=DataClassification(body.data_classification),
        organization_id=ctx.organization_id,
    )
    try:
        response = await gateway.complete(request)
    finally:
        await gateway.aclose()
    bump("model_calls_total")
    return {
        "model_used": response.model_used,
        "provider": response.provider,
        "text": response.text,
        "usage": response.usage.to_dict(),
        "cost_usd": str(response.cost_usd),
        "latency_ms": response.latency_ms,
        "routing": response.decision.to_dict() if response.decision else None,
    }


@router.get("/model/usage")
async def model_usage(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    task_id: str | None = None,
    agent_id: str | None = None,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Recent model calls with their routing reason.

    `routing_reason` is the field that matters: a run that succeeded only because
    a fallback engaged is a different result from one that succeeded on its
    primary, and conflating them flatters the evaluation numbers.
    """
    stmt = select(ModelUsage).where(ModelUsage.organization_id == ctx.organization_id)
    if task_id:
        stmt = stmt.where(ModelUsage.task_id == task_id)
    if agent_id:
        stmt = stmt.where(ModelUsage.agent_id == agent_id)
    total = int(
        (await ctx.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )
    rows = (
        (
            await ctx.session.execute(
                stmt.order_by(ModelUsage.created_at.desc(), ModelUsage.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "task_id": r.task_id,
                "agent_id": r.agent_id,
                "provider": r.provider,
                "model_used": r.model_used,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "reasoning_tokens": r.reasoning_tokens,
                "cost_usd": float(r.cost_usd),
                "latency_ms": r.latency_ms,
                "routing_reason": r.routing_reason,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/tools/invoke")
async def invoke_tool(
    body: InvokeToolRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Invoke a tool through the same gateway an agent uses.

    A human caller gets no privilege an agent does not have, and the default is
    simulation: a side-effecting tool has to be asked for explicitly.
    """
    from ai_orchestrator.domain.policy import RuleBasedPolicyEngine, default_policy_set
    from ai_orchestrator.tools.builtin import build_default_tools
    from ai_orchestrator.tools.gateway import ToolGateway

    gateway = ToolGateway(
        build_default_tools(),
        policy_engine=RuleBasedPolicyEngine(default_policy_set().rules),
    )
    try:
        run_mode = RunMode(body.run_mode)
    except ValueError as exc:
        msg = f"run_mode must be 'live' or 'simulation', got {body.run_mode!r}"
        raise ValidationError(msg, details={"field": "run_mode"}) from exc

    invocation = await gateway.invoke(
        actor=ctx.actor,
        tool_name=body.tool_name,
        arguments=body.arguments,
        organization_id=ctx.organization_id,
        task_id=body.task_id,
        execution_id=str(ExecutionId.create()),
        autonomy_level=AutonomyLevel(
            (ctx.actor.is_privileged_human and "l4_bounded_autonomous") or "l3_human_approval"
        ),
        run_mode=run_mode,
    )
    bump("tool_invocations_total")
    if not invocation.gate.allowed:
        bump("tool_denials_total")
    return {
        "tool_name": invocation.tool_name,
        "allowed": invocation.gate.allowed,
        "gate": invocation.gate.to_dict(),
        "result": invocation.result.to_dict() if invocation.result else None,
        "duration_ms": invocation.duration_ms,
        "arguments_hash": invocation.arguments_hash,
    }


@router.post("/memory")
async def write_memory(
    body: WriteMemoryRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Write a memory item. Chunked and embedded on the way in."""
    from ai_orchestrator.domain.enums import MemoryTier

    try:
        tier = MemoryTier(body.tier)
    except ValueError as exc:
        msg = f"unknown memory tier: {body.tier}"
        raise ValidationError(msg, details={"tier": body.tier}) from exc

    require_body_tenant(body.model_dump(), ctx)
    service = MemoryService(ctx.session, ctx.organization_id, embedder=HashEmbedder())
    result = await service.write(
        content=body.content,
        tier=tier,
        kind=body.kind,
        title=body.title,
        classification=DataClassification(body.classification),
        source_type=body.source_type,
        source_ref=body.source_ref,
        confidence=body.confidence,
        created_by=str(ctx.actor.id),
    )
    bump("memory_writes_total")
    return {
        "item_id": result.item_id,
        "chunk_count": result.chunk_count,
        "embedded": result.embedded,
    }


@router.post("/memory/search")
async def search_memory(
    body: SearchMemoryRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Semantic search, scoped.

    The scope is the caller's organization and nothing wider. An operator cannot
    widen it, and a memory with no owner is organisation-wide by design — which
    is why this endpoint returns provenance alongside the text: a fact and an
    agent's own summary must be distinguishable to the reader.
    """
    from ai_orchestrator.domain.contracts import MemoryScope

    require_body_tenant(body.model_dump(), ctx)
    scope = MemoryScope(
        organization_id=OrganizationId(ctx.organization_id),
        max_classification=DataClassification(body.max_classification),
        max_results=body.max_results,
    )
    service = MemoryService(ctx.session, ctx.organization_id, embedder=HashEmbedder())
    results = await service.search(body.query, scope)
    return {
        "query": body.query,
        "results": [
            {
                "item_id": r.item_id,
                "content": r.content,
                "score": round(r.score, 4),
                "tier": r.tier.value,
                "classification": r.classification.value,
                **r.to_citation(),
            }
            for r in results
        ],
        "count": len(results),
    }


@router.get("/memory/stats")
async def memory_stats(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    service = MemoryService(ctx.session, ctx.organization_id, embedder=HashEmbedder())
    return {"by_tier": await service.stats()}


__all__ = ["router"]
