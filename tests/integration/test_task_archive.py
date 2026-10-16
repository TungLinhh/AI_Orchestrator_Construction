"""Task visibility uses terminal-only reserved metadata and respects tenant scope."""

from typing import Any

import pytest
from sqlalchemy import select

from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.task_archive import ARCHIVE_SIGNATURE

pytestmark = pytest.mark.integration


async def test_archive_visibility_and_audit_retention(tenant: Any, other_tenant: Any) -> None:
    repo = TaskRepository(tenant.session, tenant.organization_id)
    archived = await repo.create(title="synthetic", goal="synthetic archive proof")
    archived.status = "completed"
    archived.output = {"evidence": "keep-me"}
    original_input = dict(archived.input)
    archived.constraints = {"budget": "retained", **ARCHIVE_SIGNATURE}
    live = await repo.create(title="live", goal="real live task")
    # A malformed or forged marker never suppresses a live task.
    live.constraints = dict(ARCHIVE_SIGNATURE)
    malformed = await repo.create(title="bad marker", goal="malformed archive task")
    malformed.status = "failed"
    malformed.constraints = {"_synthetic_archive": {"state": "archived"}}
    await tenant.session.flush()
    assert {t.id for t in await repo.all()} == {live.id, malformed.id}
    assert await repo.count_by_status() == {"created": 1, "failed": 1}
    assert len(await repo.all(include_archived=True)) == 3
    assert await repo.count_by_status(include_archived=True) == {
        "completed": 1,
        "created": 1,
        "failed": 1,
    }
    direct = await repo.get(archived.id)
    assert direct.output == {"evidence": "keep-me"}
    assert direct.input == original_input
    assert direct.constraints["budget"] == "retained"
    # Restore visibility while preserving the marker as an audit trail.
    direct.constraints = {
        **direct.constraints,
        "_synthetic_archive": {
            "version": 1,
            "state": "restored",
        },
    }
    await tenant.session.flush()
    assert len(await repo.all()) == 3
    foreign = TaskRepository(other_tenant.session, other_tenant.organization_id)
    with pytest.raises(NotFoundError):
        await foreign.get(archived.id)
    assert (
        await other_tenant.session.execute(select(Task).where(Task.id == archived.id))
    ).scalar_one_or_none() is None


async def test_reserved_archive_input_cannot_create_hidden_task(tenant: Any) -> None:
    repo = TaskRepository(tenant.session, tenant.organization_id)
    with pytest.raises(ValidationError, match="reserved"):
        await repo.create(title="hidden", goal="not allowed", constraints=ARCHIVE_SIGNATURE)
    assert await repo.count_by_status(include_archived=True) == {}


async def test_real_department_and_ceo_queries(client: Any, tenant: Any) -> None:
    from tests.integration.api_client import auth_headers

    for route in ("/api/v1/departments", "/api/v1/ceo/work"):
        response = await client.get(route, headers=auth_headers(tenant.organization_id))
        assert response.status_code == 200, response.text


async def test_cli_dryrun_archive_idempotence_restore_and_nonterminal_block(
    tenant: Any,
    tmp_path: Any,
) -> None:
    import argparse
    import hashlib
    import io
    import json
    import uuid
    import zipfile

    from scripts.archive_synthetic_tasks import digest, run

    from ai_orchestrator.audit import AuditService
    from ai_orchestrator.persistence.sources import SourceBinding, SourceFile, SourceManifest

    org = tenant.organization_id
    repo = TaskRepository(tenant.session, org)
    task = await repo.create(title="frozen test", goal="exact frozen test task")
    task.status = "completed"
    task.output = {"unchanged": "evidence"}
    task_id = task.id
    active = await repo.create(title="waiting", goal="waiting task must remain visible")
    active_id = active.id
    packet = io.BytesIO()
    with zipfile.ZipFile(packet, "w") as archive:
        archive.writestr("q.csv", "supplier,price\nA,1\n")
    content = packet.getvalue()
    source_id = "src_" + uuid.uuid4().hex
    manifest_id = "smf_" + uuid.uuid4().hex
    source_hash = hashlib.sha256(content).hexdigest()
    tenant.session.add(
        SourceFile(
            id=source_id,
            organization_id=org,
            filename="test.zip",
            media_type="application/zip",
            byte_count=len(content),
            sha256=source_hash,
            content=content,
            classification="internal",
            synthetic=False,
            origin="upload",
            uploaded_by="test",
            state="ready",
            extraction={},
        )
    )
    tenant.session.add(
        SourceManifest(
            id=manifest_id,
            organization_id=org,
            task_id=task_id,
            selection="sources",
            snapshot={},
            sha256="f" * 64,
        )
    )
    await tenant.session.flush()
    tenant.session.add(
        SourceBinding(organization_id=org, manifest_id=manifest_id, file_id=source_id)
    )
    guard = await repo.create(
        title="unsupported guard",
        goal="exact binary guard",
        input={"source_workflow": "document_review", "source_selection_mode": "sources"},
    )
    guard_id = guard.id
    guard_source_id = "src_" + uuid.uuid4().hex
    guard_content = b"SYNTHETIC-BINARY"
    tenant.session.add(
        SourceFile(
            id=guard_source_id,
            organization_id=org,
            filename="firmware.bin",
            media_type="application/octet-stream",
            byte_count=len(guard_content),
            sha256=hashlib.sha256(guard_content).hexdigest(),
            content=guard_content,
            classification="internal",
            synthetic=False,
            origin="upload",
            uploaded_by="test",
            state="unsupported",
            extraction={},
        )
    )
    await tenant.commit()
    corpus = {
        "synthetic": True,
        "cases": [
            {
                "id": "exact",
                "source": {
                    "filename": "q.csv",
                    "csv_text": "supplier,price\nA,1\n",
                },
            }
        ],
    }
    attempt = {
        "task_id": task_id,
        "case_id": "exact",
        "source_manifest_sha256": "f" * 64,
        "source_manifest": {"manifest": {"sources": [{"id": source_id, "sha256": source_hash}]}},
    }
    corpus["cases"].append(
        {
            "id": "guard",
            "kind": "unsupported_source",
            "workflow": "document_review",
            "goal": "exact binary guard",
            "source": {
                "filename": "firmware.bin",
                "bytes_hex": guard_content.hex(),
            },
            "expected": {"rejection_expected": True},
        }
    )
    receipt = {
        "run_id": "test",
        "synthetic": True,
        "corpus": {"sha256": digest(corpus)},
        "attempts": [
            attempt,
            {"task_id": active_id, "case_id": "exact"},
            {
                "task_id": guard_id,
                "case_id": "guard",
                "kind": "unsupported_source",
                "workflow": "document_review",
                "upload_id": guard_source_id,
                "upload_state": "unsupported",
                "source_manifest": {"manifest": None},
            },
        ],
    }
    contract = {"synthetic": True, "organization_id": org, "corpus_sha256": digest(corpus)}
    for name, obj in (("corpus", corpus), ("receipt", receipt), ("contract", contract)):
        (tmp_path / name).write_text(json.dumps(obj))
    args = argparse.Namespace(
        org=org,
        corpus=tmp_path / "corpus",
        run_receipt=tmp_path / "receipt",
        contract=tmp_path / "contract",
        output=tmp_path / "preview",
        apply=False,
        restore=False,
    )
    result = await run(args)
    assert not result["committed"]
    assert "nonterminal_task" in result["tasks"][1]["blocked"]
    assert result["tasks"][2]["blocked"] == ["nonterminal_task"]
    assert result["tasks"][2]["source_proof"] == "frozen_unsupported_guard_exact_bytes"
    await tenant.session.refresh(task)
    assert "_synthetic_archive" not in task.constraints
    # Test database only: use the existing state machine before archiving the guard.
    from ai_orchestrator.domain.state_machines import Transition

    await repo.transition(guard_id, Transition.CANCEL)
    await tenant.commit()
    args.apply = True
    args.output = tmp_path / "apply"
    result = await run(args)
    assert result["committed"]
    assert result["tasks"][2]["decision"] == "ARCHIVED"
    await tenant.session.refresh(task)
    assert task.constraints["_synthetic_archive"]["state"] == "archived"
    assert task.output == {"unchanged": "evidence"}
    args.output = tmp_path / "repeat"
    result = await run(args)
    assert result["tasks"][0]["decision"] == "UNCHANGED"
    args.restore = True
    args.output = tmp_path / "restore"
    await run(args)
    await tenant.session.refresh(task)
    assert task.constraints["_synthetic_archive"]["state"] == "restored"
    assert len(task.constraints["_synthetic_archive"]["history"]) == 2
    audits = await AuditService(tenant.session, org).query(task_id=task_id)
    assert {a.action for a in audits} >= {"task.archive", "task.restore"}
    assert len([a for a in audits if a.action == "task.archive"]) == 1
