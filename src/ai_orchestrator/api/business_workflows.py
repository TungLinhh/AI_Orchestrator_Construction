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
from ai_orchestrator.domain.procurement_intake import ProcurementIntake
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.domain.workflow_revision import HiringBrief
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


@router.post("/workflows/procurement", status_code=201)
async def procurement(
    body: ProcurementIntake, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_admin()
    brief = {**body.model_dump(mode="json"), "synthetic": False}
    root = await create_workflow(ctx.session, ctx.organization_id, "procurement", "live", brief)
    return {"id": root, "mode": "live", "synthetic": False, "started": False}


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: HiringBrief
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=10000)


@router.post("/workflows/{root_id}/revisions", status_code=201)
async def propose_revision(
    root_id: str, body: RevisionRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    from ai_orchestrator.application.workflow_revisions import WorkflowRevisions

    ctx.require_admin()
    return await WorkflowRevisions(ctx.session, ctx.organization_id).propose(
        root_id, body.brief, body.expected_hash, body.reason, ctx.actor
    )


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
    report = await BusinessWorkflowService(request.app.state.db, ctx.organization_id).report(
        root_id
    )
    if report["waiting"]:
        raise PreconditionError(
            "Recruitment needs new source evidence in a reviewed revision",
            details=report["waiting"],
        )
    from ai_orchestrator.application.workflow_commands import command_for

    command = await command_for(ctx.session, ctx.organization_id, root_id)
    if command and command.paused:
        raise PreconditionError("Confirm the feedback plan before resuming")
    started = await request.app.state.workflow_drivers.submit(
        ctx.organization_id, root_id, WorkflowKind.BUSINESS, session=ctx.session
    )
    return {"id": root_id, "started": started, "already_running": not started}


@router.post("/workflows/{root_id}/evidence")
async def evidence(
    root_id: str, body: EvidenceRequest, request: Request, ctx: ApiContext = Depends(get_context)
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
    await request.app.state.workflow_drivers.submit(
        ctx.organization_id, root_id, WorkflowKind.BUSINESS, session=ctx.session
    )
    return {
        "id": root_id,
        "stage_key": body.stage_key,
        "recorded": True,
        "continuation_requested": True,
    }


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


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=10, max_length=10000)


class FeedbackConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    answers: str = Field(default="", max_length=10000)
    new_revision: bool = False


@router.post("/workflows/{root_id}/feedback", status_code=202)
async def feedback(
    root_id: str, body: FeedbackRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    from ai_orchestrator.application.workflow_feedback import request_feedback

    ctx.require_admin()
    kind = await request_feedback(
        ctx.session, ctx.organization_id, root_id, body.message, ctx.actor
    )
    await ctx.session.commit()
    await request.app.state.workflow_drivers.cancel(ctx.organization_id, root_id)
    request.app.state.workflow_drivers.start(ctx.organization_id, root_id, kind, persist=False)
    return {"id": root_id, "paused": True, "feedback_recorded": True}


@router.post("/workflows/{root_id}/feedback/confirm")
async def confirm_feedback(
    root_id: str,
    body: FeedbackConfirmation,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    from ai_orchestrator.application.workflow_feedback import WorkflowFeedbackService

    ctx.require_admin()
    destination, kind = await WorkflowFeedbackService(
        request.app.state.db, ctx.organization_id
    ).confirm(
        root_id,
        ctx.actor,
        revision=body.revision,
        answers=body.answers,
        new_revision=body.new_revision,
    )
    request.app.state.workflow_drivers.start(ctx.organization_id, destination, kind, persist=False)
    return {
        "id": destination,
        "started": True,
        "revision_of": root_id if destination != root_id else None,
    }


@router.post("/workflows/{root_id}/actions/{action_id}/reconcile")
async def reconcile_action(
    root_id: str, action_id: str, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    from ai_orchestrator.application.connector_actions import ConnectorActions
    from ai_orchestrator.domain.state_machines import Transition
    from ai_orchestrator.integrations.recruitment_mail import RecruitmentMailbox
    from ai_orchestrator.persistence.models import ConnectorAction

    ctx.require_human()
    row = await ctx.session.get(ConnectorAction, action_id)
    if not row or row.root_task_id != root_id:
        raise ValidationError("Connector action does not belong to this workflow")
    root = await TaskRepository(ctx.session, ctx.organization_id).get(root_id)
    if root.status in {"completed", "failed", "canceled", "expired"}:
        raise PreconditionError("Reconciliation requires an open workflow")
    await ctx.session.commit()
    result = await ConnectorActions(request.app.state.db, ctx.organization_id).reconcile(
        action_id, RecruitmentMailbox(ctx.organization_id), ctx.actor
    )
    await request.app.state.db.bind_tenant(ctx.session, ctx.organization_id)
    if result["state"] == "confirmed" or result.get("not_attempted"):
        task = await TaskRepository(ctx.session, ctx.organization_id).get(result["stage_task_id"])
        if task.status == "blocked":
            await TaskRepository(ctx.session, ctx.organization_id).transition(
                task.id, Transition.ASSIGN
            )
            task.constraints = {
                k: v for k, v in task.constraints.items() if k != "controller_interrupted"
            }
    return result


class FeedbackAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    answers: str = Field(min_length=5, max_length=10000)


@router.post("/workflows/{root_id}/feedback/answer", status_code=202)
async def answer_feedback(
    root_id: str, body: FeedbackAnswer, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    from ai_orchestrator.application.workflow_feedback import WorkflowFeedbackService

    ctx.require_admin()
    kind = await WorkflowFeedbackService(request.app.state.db, ctx.organization_id).answer(
        root_id, ctx.actor, revision=body.revision, answers=body.answers
    )
    request.app.state.workflow_drivers.start(ctx.organization_id, root_id, kind, persist=False)
    return {"id": root_id, "answers_recorded": True}
