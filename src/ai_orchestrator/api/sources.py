"""Upload/select sources, run bounded document workflows, download accepted artifacts."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from ai_orchestrator.api.deps import (
    ApiContext,
    check_idempotency,
    get_context,
    idem_key,
    store_idempotency,
)
from ai_orchestrator.api.tasks import _chief_agent_id, _requester_id, _task_dict
from ai_orchestrator.application.source_intake import SourceIntake, file_metadata
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError, ValidationError
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.source_report import report_schema
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Execution, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.sources import SourceArtifact, SourceFile, SourceManifest

router = APIRouter(tags=["sources"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["sources", "mock"]
    file_ids: list[str] = Field(default_factory=list, max_length=10)


class SourceWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1, max_length=20000)
    workflow: Literal["document_review", "quote_comparison"]
    selection: Selection
    owner_agent_id: str | None = None


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=10, max_length=1000)


@router.get("/source-files")
async def list_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    stmt = select(SourceFile).where(SourceFile.organization_id == ctx.organization_id)
    total = await ctx.session.scalar(select(func.count()).select_from(stmt.subquery()))
    files = (
        await ctx.session.scalars(
            stmt.order_by(SourceFile.created_at.desc(), SourceFile.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [file_metadata(f) for f in files],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/source-files", status_code=201)
async def upload_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    classification: Annotated[DataClassification, Form()] = DataClassification.INTERNAL,
    org_unit_id: Annotated[str | None, Form()] = None,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    from ai_orchestrator.ingest.source_files import MAX_BYTES

    ctx.require_human()
    content = await file.read(MAX_BYTES + 1)
    async with request.app.state.source_parser_slots:
        source = await SourceIntake(ctx.session, ctx.organization_id).upload(
            file.filename or "upload",
            content,
            actor=ctx.actor,
            classification=classification,
            unit=org_unit_id,
        )
    return file_metadata(source)


@router.post("/source-files/mock", status_code=201)
async def mock_files(
    request: Request,
    workflow: Literal["document_review", "quote_comparison"] = "document_review",
    seed: int = Query(1, ge=1, le=999999),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    ctx.require_human()
    async with request.app.state.source_parser_slots:
        files = await SourceIntake(ctx.session, ctx.organization_id).mock(workflow, ctx.actor, seed)
    return {"items": [file_metadata(f) for f in files], "synthetic": True}


@router.get("/source-files/{file_id}")
async def get_file(file_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    file = await SourceIntake(ctx.session, ctx.organization_id).file(file_id)
    return {**file_metadata(file), "extraction": file.extraction}


@router.get("/source-files/{file_id}/download")
async def download_file(file_id: str, ctx: ApiContext = Depends(get_context)) -> Response:
    file = await SourceIntake(ctx.session, ctx.organization_id).file(file_id)
    if hashlib.sha256(file.content).hexdigest() != file.sha256:
        raise PreconditionError("Source content integrity mismatch")
    from ai_orchestrator.ingest.source_files import MEDIA

    suffix = Path(file.filename).suffix.lower()
    suffix = suffix if suffix in MEDIA else ""
    return Response(
        file.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (f'attachment; filename="source-{file.id}{suffix}"'),
            "X-Content-Type-Options": "nosniff",
            "ETag": '"' + file.sha256 + '"',
        },
    )


@router.post("/source-files/{file_id}/confirm", status_code=201)
async def confirm_file(
    file_id: str, body: ReviewRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_human()
    intake = SourceIntake(ctx.session, ctx.organization_id)
    file = await intake.file(file_id)
    if file.state != "needs_review" or not file.extraction.get("segments"):
        raise PreconditionError("Only a readable extraction awaiting review can be confirmed")
    await intake.admit(file.byte_count)
    extraction = copy.deepcopy(file.extraction)
    extraction.update(
        state="ready", reviewed_from=file.id, reviewed_by=str(ctx.actor.id), review_note=body.note
    )
    reviewed = SourceFile(
        id=make_id("src"),
        organization_id=ctx.organization_id,
        org_unit_id=file.org_unit_id,
        filename=file.filename,
        media_type=file.media_type,
        byte_count=file.byte_count,
        sha256=file.sha256,
        content=file.content,
        classification=file.classification,
        synthetic=file.synthetic,
        origin=file.origin,
        uploaded_by=str(ctx.actor.id),
        state="ready",
        extraction=extraction,
    )
    ctx.session.add(reviewed)
    await ctx.session.flush()
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="source.extraction_reviewed",
        resource_type="source",
        resource_id=reviewed.id,
        context={"previous": file.id, "note": body.note},
    )
    return file_metadata(reviewed)


@router.post("/source-workflows", status_code=201)
async def create_workflow(
    body: SourceWorkflowRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_human()
    payload = body.model_dump()
    key = idem_key(request)
    replayed = await check_idempotency(ctx, key=key, operation="source_workflow", payload=payload)
    if replayed is not None:
        return {**replayed["response"], "idempotent_replay": True}
    owner_id = body.owner_agent_id or await _chief_agent_id(ctx)
    owner = await ctx.session.scalar(
        select(Agent).where(Agent.organization_id == ctx.organization_id, Agent.id == owner_id)
    )
    if owner is None or owner.lifecycle_status != "active":
        raise ValidationError("Choose an active owner before creating the workflow")
    task = await TaskRepository(ctx.session, ctx.organization_id).create(
        title=body.title,
        goal=body.goal,
        task_type="analysis",
        owner_agent_id=owner.id,
        org_unit_id=owner.org_unit_id,
        requester_id=await _requester_id(ctx),
        requester_type=ctx.actor.kind.value,
        input={"source_workflow": body.workflow, "source_selection_mode": body.selection.mode},
        expected_output_schema=report_schema(body.workflow == "quote_comparison"),
        budget_limit_tokens=32000,
        allow_parallel=True,
    )
    if body.selection.file_ids:
        try:
            await SourceIntake(ctx.session, ctx.organization_id).bind(
                task, body.selection.file_ids, body.selection.mode
            )
        except PreconditionError as exc:
            await TaskRepository(ctx.session, ctx.organization_id).transition(
                task.id, Transition.REQUEST_INPUT
            )
            task.last_error = exc.message
    else:
        await TaskRepository(ctx.session, ctx.organization_id).transition(
            task.id, Transition.REQUEST_INPUT
        )
    response = _task_dict(task)
    await store_idempotency(
        ctx,
        key=key,
        operation="source_workflow",
        payload=payload,
        resource_id=task.id,
        response=response,
    )
    return response


@router.post("/tasks/{task_id}/sources")
async def bind_sources(
    task_id: str, body: Selection, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_human()
    task = await TaskRepository(ctx.session, ctx.organization_id).get(task_id)
    if task.input.get("source_workflow") not in {
        "document_review",
        "quote_comparison",
    } or task.status not in {"created", "assigned", "waiting_for_input"}:
        raise PreconditionError("Sources can only be bound to an unexecuted source workflow")
    if await ctx.session.scalar(
        select(Execution.id)
        .where(Execution.organization_id == ctx.organization_id, Execution.task_id == task_id)
        .limit(1)
    ):
        raise PreconditionError("Create a revision instead of changing an executed task")
    manifest = await SourceIntake(ctx.session, ctx.organization_id).bind(
        task, body.file_ids, body.mode
    )
    if task.status == "waiting_for_input":
        await TaskRepository(ctx.session, ctx.organization_id).transition(
            task.id, Transition.RESUME
        )
    task.last_error = None
    return {"manifest_id": manifest.id, "sha256": manifest.sha256, "task_id": task_id}


@router.get("/tasks/{task_id}/sources")
async def task_sources(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    await TaskRepository(ctx.session, ctx.organization_id).get(task_id)
    manifest = await ctx.session.scalar(
        select(SourceManifest).where(
            SourceManifest.organization_id == ctx.organization_id, SourceManifest.task_id == task_id
        )
    )
    return {
        "manifest": {**manifest.snapshot, "id": manifest.id, "sha256": manifest.sha256}
        if manifest
        else None
    }


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    task = await TaskRepository(ctx.session, ctx.organization_id).get(task_id)
    if task.status != "completed":
        return {
            "items": [],
            "reason": "Artifacts become downloadable only after validated completion",
        }
    files = (
        await ctx.session.scalars(
            select(SourceArtifact)
            .where(
                SourceArtifact.organization_id == ctx.organization_id,
                SourceArtifact.task_id == task_id,
            )
            .order_by(SourceArtifact.format)
        )
    ).all()
    return {
        "items": [
            {
                "id": f.id,
                "format": f.format,
                "filename": f.filename,
                "byte_count": len(f.content),
                "sha256": f.sha256,
                "manifest_id": f.manifest_id,
            }
            for f in files
        ]
    }


@router.get("/source-artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, ctx: ApiContext = Depends(get_context)) -> Response:
    artifact = await ctx.session.scalar(
        select(SourceArtifact)
        .join(
            Task,
            (Task.id == SourceArtifact.task_id)
            & (Task.organization_id == SourceArtifact.organization_id),
        )
        .where(
            SourceArtifact.organization_id == ctx.organization_id,
            SourceArtifact.id == artifact_id,
            Task.status == "completed",
        )
    )
    if artifact is None:
        raise NotFoundError("Artifact not found", resource_type="artifact", resource_id=artifact_id)
    if hashlib.sha256(artifact.content).hexdigest() != artifact.sha256:
        raise PreconditionError("Artifact integrity mismatch")
    return Response(
        artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-Content-Type-Options": "nosniff",
            "ETag": '"' + artifact.sha256 + '"',
        },
    )
