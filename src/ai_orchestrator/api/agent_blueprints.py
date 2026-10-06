"""Human-reviewed agent plans and their owned workflows."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.application.agent_blueprints import AgentBlueprintService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.agent_blueprint import AgentBlueprint
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.repositories.task import TaskRepository

router = APIRouter(prefix="/agent-blueprints", tags=["agent-blueprints"])


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=180)
    department: Literal["hr", "procurement", "design", "qa", "finance", "sales"]
    definition_id: str
    org_unit_id: str | None = None
    parent_agent_id: str | None = None
    model_profile: str = "primary"
    mandate: str = Field(min_length=10, max_length=12000)


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    plan: AgentBlueprint


class InputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, str] = Field(max_length=20)


@router.get("")
async def listing(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(Task)
                .where(
                    Task.organization_id == ctx.organization_id,
                    Task.input["agent_blueprint_draft"].as_boolean().is_(True),
                )
                .order_by(Task.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "revision": t.output.get("revision"),
                "provisioned": t.output.get("provisioned"),
            }
            for t in rows
        ]
    }


@router.post("", status_code=201)
async def draft(
    body: DraftRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    database = request.app.state.db
    async with database.committing_tenant_session(ctx.organization_id) as session:
        service = AgentBlueprintService(session, ctx.organization_id, database)
        row = await service.draft(body.model_dump())
        result = await service.report(row.id)
        await session.commit()
        return result


@router.get("/workflows/{root_id}")
async def workflow(root_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    root = await TaskRepository(ctx.session, ctx.organization_id).get(root_id)
    if not root.input.get("agent_workflow") or root.parent_task_id:
        raise ValidationError("Not an agent workflow root")
    rows = (
        (
            await ctx.session.execute(
                select(Task)
                .where(Task.organization_id == ctx.organization_id, Task.parent_task_id == root.id)
                .order_by(Task.created_at, Task.id)
            )
        )
        .scalars()
        .all()
    )
    completed = {t.id for t in rows if t.status == "completed"}
    plan = AgentBlueprint.model_validate(root.input["plan"])
    next_step = next(
        (
            step
            for step, ids in zip(plan.steps, root.input["step_ids"], strict=True)
            if ids["id"] not in completed
        ),
        None,
    )
    missing = (
        [
            key
            for key in next_step.input_keys
            if key in plan.required_inputs and not root.input["inputs"].get(key)
        ]
        if next_step
        else []
    )
    return {
        "id": root.id,
        "status": root.status,
        "lifecycle": root.constraints.get("workflow_lifecycle", {}),
        "error": root.last_error or root.constraints.get("workflow_lifecycle", {}).get("reason"),
        "waiting_step": next_step.key if next_step and missing else None,
        "missing_inputs": missing,
        "inputs_locked": bool(completed),
        "owner_agent_id": root.owner_agent_id,
        "plan": root.input["plan"],
        "inputs": root.input["inputs"],
        "steps": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "output": t.output,
                "error": t.last_error,
            }
            for t in rows
        ],
    }


@router.post("/workflows/{root_id}/inputs")
async def inputs(
    root_id: str, body: InputRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    root = (
        await ctx.session.execute(
            select(Task)
            .where(Task.organization_id == ctx.organization_id, Task.id == root_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if root is None or not root.input.get("agent_workflow") or root.parent_task_id:
        raise ValidationError("Not an agent workflow root")
    if root.status != "waiting_for_input":
        raise PreconditionError("Inputs are immutable after the workflow starts")
    plan = AgentBlueprint.model_validate(root.input["plan"])
    if (
        not body.inputs
        or not set(body.inputs) <= set(plan.required_inputs)
        or any(not v.strip() or len(v) > 100000 for v in body.inputs.values())
    ):
        raise ValidationError("Provide declared input keys as non-empty source text")
    completed = (
        await ctx.session.execute(
            select(Task.id)
            .where(
                Task.organization_id == ctx.organization_id,
                Task.parent_task_id == root.id,
                Task.status == "completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if completed and any(
        key in root.input["inputs"] and root.input["inputs"][key] != value
        for key, value in body.inputs.items()
    ):
        raise PreconditionError("Previously supplied sources are immutable after execution starts")
    root.input = {**root.input, "inputs": {**root.input["inputs"], **body.inputs}}
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="agent.workflow.input",
        resource_type="task",
        resource_id=root.id,
        task_id=root.id,
        context={"input_keys": sorted(body.inputs)},
    )
    return {"id": root.id, "status": root.status, "inputs_saved": sorted(body.inputs)}


@router.post("/workflows/{root_id}/run", status_code=202)
async def run(
    root_id: str, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    await workflow(root_id, ctx)
    started = await request.app.state.workflow_drivers.submit(
        ctx.organization_id, root_id, WorkflowKind.AGENT, session=ctx.session
    )
    return {"id": root_id, "started": started}


@router.get("/{draft_id}")
async def get(draft_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    return await AgentBlueprintService(ctx.session, ctx.organization_id).report(draft_id)


@router.patch("/{draft_id}")
async def edit(
    draft_id: str, body: EditRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    service = AgentBlueprintService(ctx.session, ctx.organization_id)
    await service.edit(draft_id, body.plan, ctx.actor, body.revision)
    return await service.report(draft_id)


@router.post("/{draft_id}/submit", status_code=201)
async def submit(draft_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    ctx.require_admin()
    row = await AgentBlueprintService(ctx.session, ctx.organization_id).submit(draft_id)
    return {"approval_id": row.id, "status": row.status}
