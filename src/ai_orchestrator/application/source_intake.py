"""Tenant source ingestion, immutable selection, and accepted-report exports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError, ValidationError
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.source_report import (
    compare_quotes,
    highest_classification,
    validate_report,
)
from ai_orchestrator.ingest.source_files import MAX_BYTES, MEDIA
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.sources import (
    SourceArtifact,
    SourceBinding,
    SourceFile,
    SourceManifest,
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def parse_file(filename: str, content: bytes) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ai_orchestrator.ingest.source_worker",
        filename,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(40):
            stdout, stderr = await process.communicate(content)
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode or len(stdout) > 2_000_000:
        raise ValueError(
            "Parser failed or exceeded its output/resource limit: "
            + stderr.decode(errors="replace")[-200:]
        )
    result: dict[str, Any] = json.loads(stdout)
    return result


def file_metadata(file: SourceFile) -> dict[str, Any]:
    return {
        "id": file.id,
        "filename": file.filename,
        "media_type": file.media_type,
        "byte_count": file.byte_count,
        "sha256": file.sha256,
        "extraction_sha256": digest(file.extraction),
        "reviewed_from": file.extraction.get("reviewed_from"),
        "classification": file.classification,
        "synthetic": file.synthetic,
        "origin": file.origin,
        "state": file.state,
        "org_unit_id": file.org_unit_id,
        "created_at": file.created_at.isoformat(),
        "warnings": file.extraction.get("warnings", []),
        "error": file.error,
    }


class SourceIntake:
    def __init__(self, session: AsyncSession, org: str) -> None:
        self.session, self.org = session, org

    async def file(self, file_id: str) -> SourceFile:
        file = await self.session.scalar(
            select(SourceFile).where(
                SourceFile.organization_id == self.org, SourceFile.id == file_id
            )
        )
        if file is None:
            raise NotFoundError("Source not found", resource_type="source", resource_id=file_id)
        return file

    async def admit(self, byte_count: int) -> None:
        # The same lock owns quota admission for concurrent uploads in this tenant.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:org), hashtext('source-quota'))"),
            {"org": self.org},
        )
        used = await self.session.scalar(
            select(func.coalesce(func.sum(SourceFile.byte_count), 0)).where(
                SourceFile.organization_id == self.org
            )
        )
        if int(used or 0) + byte_count > 100 * MAX_BYTES:
            raise ValidationError("Tenant source quota exceeded (1 GiB)")

    async def upload(
        self,
        filename: str,
        content: bytes,
        *,
        actor: Actor,
        classification: DataClassification,
        unit: str | None = None,
        mock: bool = False,
    ) -> SourceFile:
        if unit is not None:
            from ai_orchestrator.persistence.models import OrgUnit

            if not await self.session.scalar(
                select(OrgUnit.id).where(OrgUnit.organization_id == self.org, OrgUnit.id == unit)
            ):
                raise ValidationError("Source unit does not belong to this tenant")
        filename = Path(filename.replace("\\", "/")).name[:255]
        if not filename or not content or len(content) > MAX_BYTES:
            raise ValidationError("Choose a nonempty file of at most 10 MiB")
        await self.admit(len(content))
        try:
            extraction = await parse_file(filename, content)
        except (TimeoutError, ValueError) as exc:
            extraction = {
                "state": "failed",
                "segments": [],
                "tables": [],
                "warnings": [],
                "error": str(exc),
            }
        file = SourceFile(
            id=make_id("src"),
            organization_id=self.org,
            org_unit_id=unit,
            filename=filename,
            media_type=MEDIA.get(Path(filename).suffix.lower(), "application/octet-stream"),
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            classification=classification.value,
            synthetic=mock,
            origin="mock" if mock else "upload",
            uploaded_by=str(actor.id),
            state=extraction["state"],
            extraction=extraction,
            error=extraction.get("error"),
        )
        self.session.add(file)
        await self.session.flush()
        await AuditService(self.session, self.org).record(
            actor=actor,
            action="source.ingested",
            resource_type="source",
            resource_id=file.id,
            context={k: v for k, v in file_metadata(file).items() if k != "error"},
        )
        return file

    async def mock(self, workflow: str, actor: Actor, seed: int = 1) -> list[SourceFile]:
        # Actual files, never a dictionary substituted for the parser path.
        body = (
            "quote_id,item,quantity,unit_price,delivery,certified\n"
            f"A,Valve-{seed},12,120000,80000,true\n"
            f"B,Valve-{seed},12,110000,0,false\n"
            f"C,Valve-{seed},12,130000,0,true\n"
        ).encode()
        files = [
            await self.upload(
                f"mock-quotes-{seed}.csv",
                body,
                actor=actor,
                classification=DataClassification.INTERNAL,
                mock=True,
            )
        ]
        if workflow == "document_review":
            files.append(
                await self.upload(
                    f"mock-brief-{seed}.txt",
                    (
                        "DỮ LIỆU MOCK. Cần 12 van có chứng chỉ. Chưa đặt hàng, "
                        "chưa có phê duyệt mua hàng. Hãy chỉ rõ nhà cung cấp thiếu chứng chỉ.\n"
                    ).encode(),
                    actor=actor,
                    classification=DataClassification.INTERNAL,
                    mock=True,
                )
            )
        return files

    async def bind(self, task: Task, ids: list[str], selection: str) -> SourceManifest:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:org), hashtext(:task))"),
            {"org": self.org, "task": task.id},
        )
        if not ids or len(set(ids)) != len(ids) or len(ids) > 10:
            raise ValidationError("Select 1-10 distinct source files")
        if await self.session.scalar(
            select(SourceManifest.id).where(
                SourceManifest.organization_id == self.org, SourceManifest.task_id == task.id
            )
        ):
            raise PreconditionError(
                "This task already has an immutable manifest; create a revision"
            )
        files = [await self.file(id) for id in ids]
        if any(f.state != "ready" for f in files):
            raise PreconditionError("Every source must be readable and reviewed before running")
        if selection == "mock" and any(f.origin != "mock" for f in files):
            raise ValidationError("Mock selection accepts only files generated by this system")
        if sum(len(s["text"]) for f in files for s in f.extraction["segments"]) > 50_000:
            raise ValidationError(
                "Selected context exceeds 50000 characters; select fewer/smaller sources"
            )
        if task.input.get("source_workflow") == "quote_comparison":
            try:
                compare_quotes([table for f in files for table in f.extraction.get("tables", [])])
            except ValueError as exc:
                raise PreconditionError(str(exc)) from exc
        if sum(f.byte_count for f in files) > 20 * 1024 * 1024:
            raise ValidationError("Selected source packet exceeds 20 MiB")
        snapshot = {"version": 1, "sources": [file_metadata(f) for f in files]}
        manifest = SourceManifest(
            id=make_id("smf"),
            organization_id=self.org,
            task_id=task.id,
            selection=selection,
            snapshot=snapshot,
            sha256=digest(snapshot),
        )
        self.session.add(manifest)
        await self.session.flush()
        self.session.add_all(
            [
                SourceBinding(organization_id=self.org, manifest_id=manifest.id, file_id=f.id)
                for f in files
            ]
        )
        await self.session.flush()
        return manifest

    async def context(self, task: Task) -> dict[str, Any]:
        manifest = await self.session.scalar(
            select(SourceManifest).where(
                SourceManifest.organization_id == self.org, SourceManifest.task_id == task.id
            )
        )
        if manifest is None:
            raise PreconditionError("Select source files or generate mock files before running")
        if digest(manifest.snapshot) != manifest.sha256:
            raise PreconditionError("Source manifest integrity mismatch")
        files = []
        for entry in manifest.snapshot["sources"]:
            file = await self.file(entry["id"])
            if (
                hashlib.sha256(file.content).hexdigest() != entry["sha256"]
                or file.sha256 != entry["sha256"]
                or digest(file.extraction) != entry["extraction_sha256"]
            ):
                raise PreconditionError("Source content integrity mismatch")
            files.append(
                {
                    "id": file.id,
                    "filename": file.filename,
                    "segments": file.extraction["segments"],
                    "tables": file.extraction.get("tables", []),
                    "warnings": file.extraction.get("warnings", []),
                }
            )
        expected = None
        if task.input["source_workflow"] == "quote_comparison":
            expected = compare_quotes([table for f in files for table in f["tables"]])
        return {
            "manifest": {**manifest.snapshot, "id": manifest.id, "sha256": manifest.sha256},
            "sources": files,
            "expected": expected,
            "classification": highest_classification(
                [entry["classification"] for entry in manifest.snapshot["sources"]]
            ),
        }

    async def export(
        self, task: Task, output: dict[str, Any], source_context: dict[str, Any]
    ) -> None:
        from ai_orchestrator.application.source_exports import render_exports

        validate_report(output, source_context["sources"], source_context["expected"])
        if await self.session.scalar(
            select(SourceArtifact.id)
            .where(SourceArtifact.organization_id == self.org, SourceArtifact.task_id == task.id)
            .limit(1)
        ):
            return
        originals = []
        for entry in source_context["manifest"]["sources"]:
            file = await self.file(entry["id"])
            originals.append((file.id + "/" + file.filename, file.content))
        formats = await asyncio.to_thread(
            render_exports, task.title, output, source_context["manifest"], originals
        )
        for extension, (content, media_type) in formats.items():
            self.session.add(
                SourceArtifact(
                    id=make_id("art"),
                    organization_id=self.org,
                    task_id=task.id,
                    manifest_id=source_context["manifest"]["id"],
                    format=extension,
                    filename=f"report-{task.id}.{extension}",
                    media_type=media_type,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        await self.session.flush()
