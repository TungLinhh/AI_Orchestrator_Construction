"""Read projections for operator controls; all mutations retain their existing gates."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.domain.state_machines import AGENT_TRANSITIONS
from ai_orchestrator.tools.builtin import build_default_tools

router = APIRouter(tags=["console"])

# Only explicit columns: definition prompts are useful; credentials and arbitrary
# organization settings must never enter a generic table projection.
_CATALOGUES = {
    "procedures": "SELECT id, code, name_vi, name_en, block, department, owner_role_key, "
    "related_gate_codes, source FROM sop_definitions",
    "policies": "SELECT id, action_class, name_vi, max_level, is_hard_block, rationale, "
    "approved_by, approved_at, source FROM autonomy_policies",
    "definitions": "SELECT id, name, version, role_id, system_instructions, model_profile, "
    "allowed_child_roles, allowed_task_types, memory_policy, escalation_policy, "
    "prompt_version, is_active FROM agent_definitions",
}


@router.get("/console/context")
async def console_context(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The caller's controls and legal transitions, derived from the domain."""
    return {
        "organization_id": ctx.organization_id,
        "actor_id": str(ctx.actor.id),
        "actor_kind": ctx.actor.kind.value,
        "can_administer": ctx.is_admin,
        "can_approve": ctx.actor.kind.value == "human",
        "agent_transitions": {
            state.value: [event.value for event in transitions]
            for state, transitions in AGENT_TRANSITIONS.items()
        },
        "runtime_tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "risk_level": tool.risk.value,
                "effect_class": tool.effect_class.value,
                "requires_approval": tool.requires_approval,
            }
            for tool in build_default_tools().all()
        ],
    }


@router.get("/console/catalogue")
async def console_catalogue(
    kind: Literal["procedures", "policies", "definitions"],
    id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Tenant-scoped, reloadable catalogue details and bounded pagination."""
    statement = _CATALOGUES[kind] + " WHERE organization_id = CAST(:org AS varchar(40))"
    if id:
        statement += " AND id = :id"
    params = {"org": ctx.organization_id, "id": id, "limit": limit, "offset": offset}
    total = int(
        (
            await ctx.session.execute(
                text("SELECT count(*) FROM (" + statement + ") AS catalogue"),  # noqa: S608
                params,
                # statement contains only the Literal-selected template; values are bound.
            )
        ).scalar_one()
    )
    rows = (
        (
            await ctx.session.execute(
                text(statement + " ORDER BY id LIMIT :limit OFFSET :offset"), params
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
    }
