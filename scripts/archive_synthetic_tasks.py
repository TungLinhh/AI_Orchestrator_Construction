#!/usr/bin/env python
"""Preview or reversibly archive exact receipt tasks. Never deletes evidence.

--apply changes only terminal task constraints, after writing an exclusive before
snapshot. --restore restores visibility for the same receipt. Default is dry run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import func, or_, select

from ai_orchestrator.audit import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Approval, Execution, Task, TaskDependency
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.persistence.sources import SourceBinding, SourceFile, SourceManifest
from ai_orchestrator.persistence.task_archive import ARCHIVE_KEY, TERMINAL


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def verify_source(content: bytes, case: dict[str, Any]) -> bool:
    """Compare every payload member with the frozen synthetic corpus.

    DOCX ZIP metadata varies by creation time; compare its complete paragraph
    text and refuse embedded media/objects or extra source packet members.
    """
    source = case.get("source", {})
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as packet:
            expected_name = source.get("entry_name") or source.get("filename")
            if packet.namelist() != [expected_name]:
                return False
            if packet.getinfo(expected_name).file_size > 10 * 1024 * 1024:
                return False
            payload = packet.read(expected_name)
        if "csv_text" in source:
            return payload == source["csv_text"].encode()
        with zipfile.ZipFile(io.BytesIO(payload)) as doc:
            if any(name.startswith(("word/media/", "word/embeddings/")) for name in doc.namelist()):
                return False
            if doc.getinfo("word/document.xml").file_size > 10 * 1024 * 1024:
                return False
            root = ElementTree.fromstring(doc.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = ["".join(p.itertext()) for p in root.findall(".//w:body/w:p", ns)]
        all_text = [t.text or "" for t in root.findall(".//w:t", ns)]
        return paragraphs == source.get("paragraphs") and "".join(all_text) == "".join(paragraphs)
    except ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError:
        return False


def verify_unsupported_source(
    task_input: dict[str, Any],
    task_goal: str,
    case: dict[str, Any],
    attempt: dict[str, Any],
    source: SourceFile | None,
) -> bool:
    """Only the frozen binary guard case may lack a bound manifest."""
    try:
        expected_bytes = bytes.fromhex(case["source"]["bytes_hex"])
    except KeyError, TypeError, ValueError:
        return False
    return bool(
        case.get("kind") == attempt.get("kind") == "unsupported_source"
        and case.get("workflow") == attempt.get("workflow") == task_input.get("source_workflow")
        and task_input.get("source_selection_mode") == "sources"
        and task_goal == case.get("goal")
        and case.get("expected", {}).get("rejection_expected") is True
        and attempt.get("upload_state") == "unsupported"
        and attempt.get("source_manifest") == {"manifest": None}
        and source is not None
        and attempt.get("upload_id") == source.id
        and attempt.get("source_ids", [source.id]) == [source.id]
        and source.origin == "upload"
        and source.state == "unsupported"
        and source.filename == case["source"].get("filename")
        and source.content == expected_bytes
        and hashlib.sha256(source.content).hexdigest() == source.sha256
    )


def load_proof(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], str]:
    receipt = json.loads(args.run_receipt.read_bytes())
    contract = json.loads(args.contract.read_bytes())
    corpus = json.loads(args.corpus.read_bytes())
    corpus_hash = digest(corpus)
    if not (
        receipt.get("synthetic") is True
        and contract.get("synthetic") is True
        and corpus.get("synthetic") is True
        and contract.get("organization_id") == args.org
        and contract.get("corpus_sha256") == corpus_hash == receipt.get("corpus", {}).get("sha256")
    ):
        raise ValueError("tenant or frozen synthetic corpus/contract/receipt proof mismatch")
    cases = {c["id"]: c for c in corpus["cases"]}
    attempts = {}
    for attempt in receipt["attempts"]:
        task_id = attempt.get("task_id")
        if not task_id:
            continue
        if task_id in attempts or attempt.get("case_id") not in cases:
            raise ValueError("duplicate task ID or unknown corpus case")
        attempts[task_id] = {"attempt": attempt, "case": cases[attempt["case_id"]]}
    if not attempts:
        raise ValueError("receipt has no exact task candidates")
    return receipt, attempts, hashlib.sha256(args.run_receipt.read_bytes()).hexdigest()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    receipt, candidates, receipt_hash = load_proof(args)
    database = Database.from_settings()
    report: dict[str, Any] = {
        "organization_id": args.org,
        "receipt_sha256": receipt_hash,
        "run_id": receipt["run_id"],
        "apply_requested": args.apply,
        "operation": "restore" if args.restore else "archive",
        "tasks": [],
    }
    try:
        await database.assert_app_role_is_least_privilege()
        async with database.tenant_session(args.org) as session:
            for task_id, proof in candidates.items():
                task = (
                    await session.execute(
                        select(Task)
                        .where(Task.organization_id == args.org, Task.id == task_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                entry: dict[str, Any] = {"task_id": task_id, "blocked": []}
                report["tasks"].append(entry)
                if task is None:
                    entry["blocked"].append("missing_or_foreign_task")
                    entry["decision"] = "BLOCKED"
                    continue
                entry["before"] = {c.name: getattr(task, c.name) for c in Task.__table__.columns}
                entry["before_sha256"] = digest(
                    json.loads(json.dumps(entry["before"], default=str))
                )
                marker = task.constraints.get(ARCHIVE_KEY, {})
                if not isinstance(marker, dict) or not isinstance(marker.get("history", []), list):
                    entry["blocked"].append("malformed_archive_marker")
                    entry["decision"] = "BLOCKED"
                    continue
                if task.status not in TERMINAL:
                    entry["blocked"].append("nonterminal_task")
                if marker and marker.get("receipt_sha256") != receipt_hash:
                    entry["blocked"].append("different_archive_receipt")
                dependencies = (
                    await session.execute(
                        select(func.count())
                        .select_from(TaskDependency)
                        .where(
                            TaskDependency.organization_id == args.org,
                            or_(
                                TaskDependency.task_id == task_id,
                                TaskDependency.depends_on_task_id == task_id,
                            ),
                        )
                    )
                ).scalar_one()
                children = (
                    await session.execute(
                        select(func.count())
                        .select_from(Task)
                        .where(Task.organization_id == args.org, Task.parent_task_id == task_id)
                    )
                ).scalar_one()
                pending = (
                    await session.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.organization_id == args.org,
                            Approval.task_id == task_id,
                            Approval.status == "pending",
                        )
                    )
                ).scalar_one()
                active_runs = (
                    await session.execute(
                        select(func.count())
                        .select_from(Execution)
                        .where(
                            Execution.organization_id == args.org,
                            Execution.task_id == task_id,
                            Execution.status == "running",
                        )
                    )
                ).scalar_one()
                if dependencies or children or pending or active_runs or task.parent_task_id:
                    entry["blocked"].append(
                        "linked_tasks_dependencies_pending_approval_or_active_execution"
                    )
                manifest = (
                    await session.execute(
                        select(SourceManifest).where(
                            SourceManifest.organization_id == args.org,
                            SourceManifest.task_id == task_id,
                        )
                    )
                ).scalar_one_or_none()
                attempt = proof["attempt"]
                if not manifest and proof["case"].get("kind") == "unsupported_source":
                    uploaded_id = attempt.get("upload_id")
                    source = (
                        await session.execute(
                            select(SourceFile).where(
                                SourceFile.organization_id == args.org, SourceFile.id == uploaded_id
                            )
                        )
                    ).scalar_one_or_none()
                    bound_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(SourceBinding)
                            .where(
                                SourceBinding.organization_id == args.org,
                                SourceBinding.file_id == uploaded_id,
                            )
                        )
                    ).scalar_one()
                    entry["source_flags"] = {source.id: source.synthetic} if source else {}
                    if bound_count or not verify_unsupported_source(
                        task.input, task.goal, proof["case"], attempt, source
                    ):
                        entry["blocked"].append("unsupported_guard_provenance_mismatch")
                    else:
                        entry["source_proof"] = "frozen_unsupported_guard_exact_bytes"
                elif not manifest or manifest.sha256 != attempt.get("source_manifest_sha256"):
                    entry["blocked"].append("source_manifest_mismatch")
                else:
                    sources = (
                        (
                            await session.execute(
                                select(SourceFile)
                                .join(
                                    SourceBinding,
                                    (SourceBinding.file_id == SourceFile.id)
                                    & (SourceBinding.organization_id == SourceFile.organization_id),
                                )
                                .where(
                                    SourceBinding.organization_id == args.org,
                                    SourceBinding.manifest_id == manifest.id,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    frozen_sources = (attempt.get("source_manifest", {}).get("manifest") or {}).get(
                        "sources", []
                    )
                    expected = {s["id"]: s["sha256"] for s in frozen_sources}
                    entry["source_flags"] = {s.id: s.synthetic for s in sources}
                    if (
                        not sources
                        or {s.id: s.sha256 for s in sources} != expected
                        or any(
                            hashlib.sha256(s.content).hexdigest() != s.sha256
                            or not verify_source(s.content, proof["case"])
                            for s in sources
                        )
                    ):
                        entry["blocked"].append("source_bytes_do_not_match_frozen_synthetic_corpus")
                desired = "restored" if args.restore else "archived"
                if args.restore and not marker:
                    entry["blocked"].append("no_archive_to_restore")
                entry["decision"] = (
                    "BLOCKED"
                    if entry["blocked"]
                    else ("UNCHANGED" if marker.get("state") == desired else desired.upper())
                )
            # This immutable before snapshot is written before any UPDATE/commit.
            with args.output.open("x") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
            if args.apply:
                for entry in report["tasks"]:
                    if entry["decision"] not in {"ARCHIVED", "RESTORED"}:
                        continue
                    task = (
                        await session.execute(
                            select(Task).where(
                                Task.organization_id == args.org, Task.id == entry["task_id"]
                            )
                        )
                    ).scalar_one()
                    task.constraints = {
                        **task.constraints,
                        ARCHIVE_KEY: {
                            **task.constraints.get(ARCHIVE_KEY, {}),
                            "version": 1,
                            "state": "restored" if args.restore else "archived",
                            "receipt_sha256": receipt_hash,
                            "run_id": receipt["run_id"],
                            "changed_at": utcnow().isoformat(),
                            "history": [
                                *task.constraints.get(ARCHIVE_KEY, {}).get("history", []),
                                {
                                    "operation": "restore" if args.restore else "archive",
                                    "at": utcnow().isoformat(),
                                    "receipt_sha256": receipt_hash,
                                },
                            ],
                            "before_sha256": entry["before_sha256"],
                        },
                    }
                    await AuditService(session, args.org).record(
                        actor=Actor(id="svc:synthetic-task-archive", kind=ActorType.SERVICE),
                        action="task.restore" if args.restore else "task.archive",
                        resource_type="task",
                        resource_id=task.id,
                        task_id=task.id,
                        context={
                            "receipt_sha256": receipt_hash,
                            "run_id": receipt["run_id"],
                            "before_sha256": entry["before_sha256"],
                            "source_proof": entry.get(
                                "source_proof", "frozen_corpus_content_match"
                            ),
                        },
                    )
        report["committed"] = bool(args.apply)
        return report
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--run-receipt", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "committed": report["committed"],
                "decisions": {
                    state: sum(t["decision"] == state for t in report["tasks"])
                    for state in {t["decision"] for t in report["tasks"]}
                },
            }
        )
    )


if __name__ == "__main__":
    main()
