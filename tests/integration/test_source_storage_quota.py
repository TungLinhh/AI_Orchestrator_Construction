"""Sources and accepted export binaries share a tenant-scoped admission limit."""

import asyncio

import pytest
from sqlalchemy import func, select

from ai_orchestrator.config.settings import reset_settings_cache
from ai_orchestrator.persistence.sources import SourceArtifact, SourceFile
from tests.integration.test_source_workflow import create, run
from tests.integration.test_source_workflow import source_client as fixture_source_client

pytestmark = pytest.mark.integration
source_client = fixture_source_client


def _quota(monkeypatch, byte_count):
    monkeypatch.setenv("AO_SOURCE_STORAGE_QUOTA_BYTES", str(byte_count))
    reset_settings_cache()


async def _upload(client, filename, content, **kwargs):
    return await client.post(
        "/api/v1/source-files", files={"file": (filename, content, "text/plain")}, **kwargs
    )


async def test_concurrent_uploads_cannot_oversubscribe_one_tenant(
    source_client, tenant, monkeypatch
):
    client, _ = source_client
    _quota(monkeypatch, 20)
    responses = await asyncio.gather(
        _upload(client, "first.txt", b"0123456789ABCDEF"),
        _upload(client, "second.txt", b"0123456789ABCDEF"),
    )
    assert sorted(response.status_code for response in responses) == [201, 422]
    used = await tenant.session.scalar(select(func.sum(SourceFile.byte_count)))
    assert used == 16


async def test_export_quota_failure_does_not_publish_partial_artifacts(
    source_client, tenant, monkeypatch
):
    client, provider = source_client
    source = await client.post("/api/v1/source-files/mock?workflow=quote_comparison")
    assert source.status_code == 201, source.text
    file = source.json()["items"][0]
    task = await create(client, [file["id"]], workflow="quote_comparison", mode="mock")
    _quota(monkeypatch, file["byte_count"] + 100)
    settled = await run(client, task)
    assert settled["status"] == "failed", settled
    assert "storage quota exceeded" in str(settled["last_error"]), settled
    assert settled["failure_category"] == "validation_error", settled
    assert len(provider.calls) == 1
    count = await tenant.session.scalar(select(func.count()).select_from(SourceArtifact))
    assert count == 0
    # Usage is still visible, even though export could not fit. No success/artifact
    # is manufactured to conceal the already completed scripted provider call.
    ledger = (await client.get(f"/api/v1/model/usage?task_id={task['id']}")).json()["items"]
    assert ledger
    executions = (await client.get(f"/api/v1/tasks/{task['id']}/executions")).json()["items"]
    assert executions[0]["status"] == "failed"
    assert executions[0]["model_used"] == "scripted-source-report"


async def test_existing_exports_reduce_capacity_for_subsequent_uploads(
    source_client, tenant, monkeypatch
):
    client, _ = source_client
    source = await client.post("/api/v1/source-files/mock?workflow=quote_comparison")
    file = source.json()["items"][0]
    task = await create(client, [file["id"]], workflow="quote_comparison", mode="mock")
    assert (await run(client, task))["status"] == "completed"
    artifact_bytes = await tenant.session.scalar(
        select(func.sum(func.octet_length(SourceArtifact.content)))
    )
    assert artifact_bytes > 100
    _quota(monkeypatch, file["byte_count"] + artifact_bytes + 4)
    response = await _upload(client, "would-overflow.txt", b"12345")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["used_bytes"] == file["byte_count"] + artifact_bytes


async def test_one_tenants_capacity_does_not_consume_another_tenants_limit(
    source_client, tenant, other_tenant, monkeypatch
):
    client, _ = source_client
    _quota(monkeypatch, 16)
    first = await _upload(client, "tenant-a.txt", b"0123456789ABCDEF")
    second = await _upload(
        client,
        "tenant-b.txt",
        b"0123456789ABCDEF",
        headers={"x-organization-id": other_tenant.organization_id},
    )
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert await tenant.session.scalar(select(func.sum(SourceFile.byte_count))) == 16
    assert await other_tenant.session.scalar(select(func.sum(SourceFile.byte_count))) == 16


async def test_failure_after_export_flush_rolls_back_all_artifacts_and_keeps_usage(
    source_client, tenant, monkeypatch
):
    from ai_orchestrator.application.source_intake import SourceIntake

    original = SourceIntake.export

    async def fail_after_flush(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("synthetic-private-render-detail-do-not-expose")

    monkeypatch.setattr(SourceIntake, "export", fail_after_flush)
    client, provider = source_client
    source = await client.post("/api/v1/source-files/mock?workflow=quote_comparison")
    task = await create(
        client, [source.json()["items"][0]["id"]], workflow="quote_comparison", mode="mock"
    )
    settled = await run(client, task)
    assert settled["status"] == "failed", settled
    assert settled["failure_category"] == "internal_error"
    assert settled["last_error"] == "Artifact export failed"
    assert not settled["output"]
    assert len(provider.calls) == 1
    assert await tenant.session.scalar(select(func.count()).select_from(SourceArtifact)) == 0
    ledger = (await client.get(f"/api/v1/model/usage?task_id={task['id']}")).json()["items"]
    assert len(ledger) == 1


async def test_export_replay_does_not_charge_again_after_capacity_is_full(
    source_client, tenant, monkeypatch
):
    from ai_orchestrator.application.source_intake import SourceIntake
    from ai_orchestrator.persistence.models import Task

    client, provider = source_client
    source = await client.post("/api/v1/source-files/mock?workflow=quote_comparison")
    task = await create(
        client, [source.json()["items"][0]["id"]], workflow="quote_comparison", mode="mock"
    )
    assert (await run(client, task))["status"] == "completed"
    before = (await tenant.session.scalars(select(SourceArtifact))).all()
    original_hashes = {artifact.id: artifact.sha256 for artifact in before}
    stored = await tenant.session.get(Task, task["id"])
    intake = SourceIntake(tenant.session, tenant.organization_id)
    context = await intake.context(stored)
    _quota(monkeypatch, 1)
    await intake.export(stored, stored.output, context)
    after = (await tenant.session.scalars(select(SourceArtifact))).all()
    assert {artifact.id: artifact.sha256 for artifact in after} == original_hashes
    assert len(after) == 6
    assert len(provider.calls) == 1
