"""Upload/select sources, run bounded document workflows, download accepted artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from ai_orchestrator.api.deps import (
    ApiContext,
    check_idempotency,
    get_context,
    idem_key,
    store_idempotency,
)
from ai_orchestrator.api.tasks import _chief_agent_id, _requester_id, _task_dict
from ai_orchestrator.application.source_intake import SourceIntake, digest, file_metadata
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError, ValidationError
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.source_report import report_schema
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, Execution, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.sources import SourceArtifact, SourceFile, SourceManifest
from ai_orchestrator.persistence.task_archive import ARCHIVE_KEY

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


class SourceRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    selection: Selection
    reason: str = Field(min_length=10, max_length=1000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=10, max_length=1000)


async def _source_request_key(ctx: ApiContext, raw_key: str | None, namespace: str) -> str | None:
    if raw_key is None:
        return None
    if len(raw_key) > 128:
        raise ValidationError("Idempotency-Key must be at most 128 characters")
    key = namespace + ":" + digest([ctx.organization_id, raw_key])
    await ctx.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
        {"org": ctx.organization_id, "key": key},
    )
    return key


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
    raw_key = idem_key(request)
    key = await _source_request_key(ctx, raw_key, "source-workflow")
    replayed = await check_idempotency(ctx, key=key, operation="source_workflow", payload=payload)
    if replayed is None and raw_key is not None:
        # Existing responses predate tenant namespacing. RLS restricts this
        # lookup to the caller's tenant; preserve their replay contract.
        replayed = await check_idempotency(
            ctx, key=raw_key, operation="source_workflow", payload=payload
        )
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


@router.post("/tasks/{task_id}/source-revisions", status_code=201)
async def revise_sources(
    task_id: str,
    body: SourceRevisionRequest,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Create unstarted work with new sources, keeping the original as history."""
    ctx.require_human()
    raw_key = idem_key(request)
    if not raw_key:
        raise ValidationError("Source revisions require an Idempotency-Key of 1-128 characters")
    # Serialize before replay checks; each tenant owns its request namespace.
    key = await _source_request_key(ctx, raw_key, "source-revision")
    payload = {"task_id": task_id, **body.model_dump()}
    replayed = await check_idempotency(ctx, key=key, operation="source_revision", payload=payload)
    if replayed is not None:
        return {**replayed["response"], "idempotent_replay": True}
    tasks = TaskRepository(ctx.session, ctx.organization_id)
    original = await tasks.get(task_id)
    if original.input.get("source_workflow") not in {"document_review", "quote_comparison"}:
        raise PreconditionError("Only source workflows support source revisions")
    if original.status not in {"completed", "failed", "canceled", "expired"}:
        raise PreconditionError("Finish or cancel the original task before revising its sources")
    if original.input.get("business_workflow") or original.parent_task_id:
        raise PreconditionError("Revise ordered work through its workflow controller")
    previous = await ctx.session.scalar(
        select(SourceManifest).where(
            SourceManifest.organization_id == ctx.organization_id,
            SourceManifest.task_id == task_id,
        )
    )
    owner = await ctx.session.scalar(
        select(Agent).where(
            Agent.organization_id == ctx.organization_id, Agent.id == original.owner_agent_id
        )
    )
    if owner is None or owner.lifecycle_status != "active":
        raise PreconditionError("The source workflow needs an active owner before revision")
    revision = await tasks.create(
        title=original.title,
        goal=original.goal,
        task_type=original.task_type,
        owner_agent_id=owner.id,
        org_unit_id=owner.org_unit_id,
        requester_id=await _requester_id(ctx),
        requester_type=ctx.actor.kind.value,
        priority=original.priority,
        input={
            "source_workflow": original.input["source_workflow"],
            "source_selection_mode": body.selection.mode,
            "source_revision": {
                "previous_task_id": original.id,
                "previous_manifest_id": previous.id if previous else None,
                "reason": body.reason,
            },
        },
        constraints={
            key: copy.deepcopy(value)
            for key, value in original.constraints.items()
            if key != ARCHIVE_KEY
        },
        expected_output_schema=copy.deepcopy(original.expected_output_schema),
        budget_limit_tokens=original.budget_limit_tokens,
        budget_limit_usd=float(original.budget_limit_usd)
        if original.budget_limit_usd is not None
        else None,
        allow_parallel=True,
    )
    manifest = await SourceIntake(ctx.session, ctx.organization_id).bind(
        revision, body.selection.file_ids, body.selection.mode
    )
    await tasks.add_dependency(task_id=revision.id, depends_on_task_id=original.id)
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="source.workflow_revised",
        resource_type="task",
        resource_id=revision.id,
        context={
            "previous_task_id": original.id,
            "previous_manifest_id": previous.id if previous else None,
            "manifest_id": manifest.id,
            "reason": body.reason,
        },
    )
    response = _task_dict(revision)
    await store_idempotency(
        ctx,
        key=key,
        operation="source_revision",
        payload=payload,
        resource_id=revision.id,
        response=response,
    )
    return response


@router.post("/tasks/{task_id}/sources")
async def bind_sources(
    task_id: str, body: Selection, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    ctx.require_human()
    # Match TaskAttemptRunner's admission lock before inspecting task state.
    # A row lock alone cannot exclude the runner's pre-claim read.
    if not await ctx.session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
        {"org": ctx.organization_id, "key": "task-attempt:" + task_id},
    ):
        raise PreconditionError("Task execution is being admitted; attach sources before running")
    task = await ctx.session.scalar(
        select(Task)
        .where(Task.organization_id == ctx.organization_id, Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise NotFoundError("Task not found", resource_type="task", resource_id=task_id)
    source_workflow = task.input.get("source_workflow") in {
        "document_review",
        "quote_comparison",
    }
    ordinary_analysis = (
        not task.input.get("source_workflow")
        and task.task_type == "analysis"
        and not task.expected_output_schema
        and not task.input.get("business_workflow")
        and not task.parent_task_id
    )
    if not (source_workflow or ordinary_analysis) or task.status not in {
        "created",
        "assigned",
        "waiting_for_input",
    }:
        raise PreconditionError(
            "Attach sources only to an unexecuted document workflow or ordinary "
            "analysis without a declared output schema; ordered work uses its controller"
        )
    if await ctx.session.scalar(
        select(Execution.id)
        .where(Execution.organization_id == ctx.organization_id, Execution.task_id == task_id)
        .limit(1)
    ):
        raise PreconditionError("Create a revision instead of changing an executed task")
    # Binding is an explicit opt-in to the existing cited-report contract.
    # Never silently replace a declared schema or a workflow stage contract.
    manifest = await SourceIntake(ctx.session, ctx.organization_id).bind(
        task, body.file_ids, body.mode
    )
    if ordinary_analysis:
        task.input = {
            **task.input,
            "source_workflow": "document_review",
            "source_selection_mode": body.mode,
        }
        task.expected_output_schema = report_schema(False)
        await AuditService(ctx.session, ctx.organization_id).record(
            actor=ctx.actor,
            action="task.sources_attached",
            resource_type="task",
            resource_id=task.id,
            context={"manifest_id": manifest.id, "contract": "document_review"},
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


@router.get("/tasks/{task_id}/answer/download")
async def download_task_answer(task_id: str, ctx: ApiContext = Depends(get_context)) -> Response:
    """Export the persisted completed answer, never a model-supplied filesystem path.

    This is an answer export, not a registered source artifact or proof of human
    approval. All fields are retained without truncation or model interpretation.
    """
    task = await TaskRepository(ctx.session, ctx.organization_id).get(task_id)
    if task.status != "completed":
        raise PreconditionError("An answer can be downloaded only after task completion")
    output = task.output or {}
    fields = [(key, value) for key, value in output.items() if value is not None and value != ""]
    if not fields:
        raise PreconditionError("This task has no written answer to export")
    sections = []
    for key, value in fields:
        rendered = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        )
        sections.append(f"## {key}\n\n{rendered}")
    content = ("\n\n".join(sections) + "\n").encode("utf-8")
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="task-answer.md"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "ETag": '"' + hashlib.sha256(content).hexdigest() + '"',
        },
    )


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
