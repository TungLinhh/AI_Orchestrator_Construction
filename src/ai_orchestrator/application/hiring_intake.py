"""Explicit, tenant-scoped manual CV intake at the approved recruitment boundary."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from typing import Any
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import DEV_DATA_DIR
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError, ValidationError
from ai_orchestrator.integrations.recruitment_mail import store_cv
from ai_orchestrator.persistence.models import Task


async def receive_cv(
    session: AsyncSession,
    organization_id: str,
    root_id: str,
    *,
    filename: str,
    content: bytes,
    synthetic: bool,
    actor: Actor,
) -> dict[str, Any]:
    """Record a CV without running, approving, sending or changing stage outcomes."""
    acquired = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:root))"),
        {"org": organization_id, "root": root_id},
    )
    if not acquired:
        raise PreconditionError("This workflow is running; wait before uploading CVs")
    root = await session.scalar(
        select(Task)
        .where(Task.organization_id == organization_id, Task.id == root_id)
        .with_for_update()
    )
    if root is None:
        raise NotFoundError("Workflow not found")
    if (
        root.parent_task_id
        or root.input.get("business_workflow") != "mep_hiring"
        or root.input.get("mode") != "live"
        or root.status in {"completed", "failed", "canceled", "expired"}
    ):
        raise PreconditionError("Manual CV intake requires an open live HR workflow")
    if synthetic is not bool(root.input["brief"].get("synthetic", False)):
        raise ValidationError("CV provenance must match the workflow's real/synthetic label")
    stages = (
        await session.scalars(
            select(Task)
            .where(Task.organization_id == organization_id, Task.parent_task_id == root_id)
            .order_by(Task.created_at, Task.id)
            .with_for_update()
        )
    ).all()
    current = {stage.input.get("stage_key"): stage for stage in stages}
    intake = current.get("cv_intake")
    review = current.get("rubric_review")
    if (
        intake is None
        or intake.status not in {"assigned", "waiting_for_input"}
        or review is None
        or review.status != "completed"
    ):
        raise PreconditionError("Upload CVs after rubric approval and before intake executes")
    records = list(root.constraints.get("manual_cv_intake", []))
    digest = hashlib.sha256(content).hexdigest()
    existing = next((r for r in records if r["candidate_id"] == digest), None)
    if existing:
        return {"candidate_id": digest, "duplicate": True, "count": len(records), "started": False}
    if len(records) >= 50:
        raise PreconditionError("Manual intake is limited to fifty CVs per campaign")
    try:
        record = await asyncio.to_thread(store_cv, content, filename, organization_id, root_id)
    except (
        BadZipFile,
        KeyError,
        ParseError,
        UnicodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        _remove_unaccepted_file(organization_id, root_id, digest, filename)
        raise ValidationError(
            "CV cannot be read; upload a valid PDF, DOCX or UTF-8 TXT file"
        ) from exc
    except ValueError as exc:
        _remove_unaccepted_file(organization_id, root_id, digest, filename)
        raise ValidationError(str(exc)) from exc
    record.update({"source": "manual_cv", "synthetic": synthetic})
    root.constraints = {**root.constraints, "manual_cv_intake": [*records, record]}
    await AuditService(session, organization_id).record(
        actor=actor,
        action="workflow.cv.uploaded",
        resource_type="task",
        resource_id=intake.id,
        task_id=intake.id,
        context={"root": root_id, "candidate_id": record["candidate_id"], "synthetic": synthetic},
    )
    return {
        "candidate_id": record["candidate_id"],
        "duplicate": False,
        "count": len(records) + 1,
        "synthetic": synthetic,
        "started": False,
    }


def _remove_unaccepted_file(org: str, root: str, digest: str, filename: str) -> None:
    from pathlib import Path

    suffix = Path(filename).suffix.lower()
    if suffix in {".pdf", ".docx", ".txt"}:
        (DEV_DATA_DIR / "recruitment" / org / root / (digest + suffix)).unlink(missing_ok=True)
