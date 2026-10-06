"""Operator workflow controls. The same controller is used by CLI and console."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.application.business_workflow import (
    BusinessWorkflowService,
    create_workflow,
    payload_hash,
)
from ai_orchestrator.application.workflow_fixtures import hiring_fixture, procurement_fixture
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.business_workflow import RUBRIC_SPEC
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.repositories.task import TaskRepository

router = APIRouter(tags=["business-workflows"])


class ExampleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["mep_hiring", "procurement"]


class HiringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: str = Field(min_length=3, max_length=200)
    boss_brief: str = Field(min_length=20, max_length=10000)
    salary_min: int = Field(gt=0)
    salary_max: int = Field(gt=0)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/workflows/hiring", status_code=201)
async def hiring(body: HiringRequest, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    ctx.require_admin()
    from datetime import date

    try:
        date.fromisoformat(body.start_date)
    except ValueError as exc:
        raise ValidationError("Start date must be a valid calendar date") from exc
    brief = {
        **body.model_dump(),
        "synthetic": False,
        "headcount": 1,
        "rubric_spec": [{"key": key, "max_points": weight} for key, weight in RUBRIC_SPEC],
    }
    root = await create_workflow(ctx.session, ctx.organization_id, "mep_hiring", "live", brief)
    return {"id": root, "mode": "live", "synthetic": False, "started": False}


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_key: Literal[
        "interview_technical", "interview_hr", "offer_acceptance", "onboarding_evidence", "delivery"
    ]
    evidence: dict[str, Any]


@router.get("/workflows")
async def listing(
    limit: int = 50, offset: int = 0, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValidationError("Invalid workflow pagination")
    criteria = (
        Task.organization_id == ctx.organization_id,
        Task.parent_task_id.is_(None),
        Task.input.has_key("business_workflow"),
    )
    total = (
        await ctx.session.execute(select(func.count()).select_from(Task).where(*criteria))
    ).scalar_one()
    rows = (
        (
            await ctx.session.execute(
                select(Task)
                .where(*criteria)
                .order_by(Task.created_at.desc(), Task.id)
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
                "title": r.title,
                "kind": r.input["business_workflow"],
                "mode": r.input["mode"],
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/workflows/examples", status_code=201)
async def example(body: ExampleRequest, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    ctx.require_admin()
    brief = hiring_fixture() if body.kind == "mep_hiring" else procurement_fixture()
    root = await create_workflow(ctx.session, ctx.organization_id, body.kind, "simulation", brief)
    return {"id": root, "mode": "simulation", "synthetic": True, "started": False}


@router.get("/workflows/{root_id}")
async def detail(
    root_id: str, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    return await BusinessWorkflowService(request.app.state.db, ctx.organization_id).report(root_id)


@router.post("/workflows/{root_id}/run")
async def run(
    root_id: str, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    root = await TaskRepository(ctx.session, ctx.organization_id).get(root_id)
    if root.parent_task_id or not root.input.get("business_workflow"):
        raise ValidationError("Run requires a workflow root")
    if root.status in {"completed", "failed", "canceled", "expired"}:
        raise PreconditionError("This workflow is terminal; create a new run for changed inputs")
    await ctx.session.commit()
    started = request.app.state.workflow_drivers.start(
        ctx.organization_id, root_id, WorkflowKind.BUSINESS
    )
    return {"id": root_id, "started": started, "already_running": not started}


@router.post("/workflows/{root_id}/evidence")
async def evidence(
    root_id: str, body: EvidenceRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_human()
    root = await TaskRepository(ctx.session, ctx.organization_id).get(root_id)
    if (
        root.parent_task_id
        or not root.input.get("business_workflow")
        or root.status in {"completed", "failed", "canceled", "expired"}
    ):
        raise PreconditionError("Evidence requires an open workflow")
    key = "onboarding_setup" if body.stage_key == "onboarding_evidence" else body.stage_key
    stage = (
        await ctx.session.execute(
            select(Task).where(
                Task.organization_id == ctx.organization_id,
                Task.parent_task_id == root_id,
                Task.input["stage_key"].astext == key,
            )
        )
    ).scalar_one_or_none()
    if stage is None or stage.status not in {"assigned", "waiting_for_input"}:
        raise PreconditionError("Cannot replace evidence for an executed or in-flight stage")
    brief = {**root.input["brief"], body.stage_key: body.evidence}
    root.input = {**root.input, "brief": brief}
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="workflow.evidence.submitted",
        resource_type="task",
        resource_id=stage.id,
        task_id=stage.id,
        context={"stage": body.stage_key, "evidence_hash": payload_hash(body.evidence)},
    )
    return {"id": root_id, "stage_key": body.stage_key, "recorded": True}


@router.post("/workflows/{root_id}/retry", status_code=201)
async def retry(root_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    ctx.require_admin()
    source = await TaskRepository(ctx.session, ctx.organization_id).get(root_id)
    if (
        source.parent_task_id
        or not source.input.get("business_workflow")
        or source.status != "failed"
    ):
        raise PreconditionError("Retry requires a failed business workflow")
    await ctx.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:org), hashtext(:root))"),
        {"org": ctx.organization_id, "root": "retry:" + root_id},
    )
    active = (
        (
            await ctx.session.execute(
                select(Task).where(
                    Task.organization_id == ctx.organization_id,
                    Task.parent_task_id.is_(None),
                    Task.input["reuse_source"].astext == root_id,
                    Task.status.not_in(("completed", "failed", "canceled", "expired")),
                )
            )
        )
        .scalars()
        .first()
    )
    if active:
        return {
            "id": active.id,
            "mode": active.input["mode"],
            "source_root": root_id,
            "started": False,
            "already_active": True,
        }
    root = await create_workflow(
        ctx.session,
        ctx.organization_id,
        source.input["business_workflow"],
        source.input["mode"],
        source.input["brief"],
        reuse_source=root_id,
    )
    return {"id": root, "mode": source.input["mode"], "source_root": root_id, "started": False}
