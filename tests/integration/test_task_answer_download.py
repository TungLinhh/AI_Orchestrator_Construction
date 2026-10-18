"""Answer exports use completed tenant-owned persisted data, never tool paths."""

import hashlib

import pytest

from ai_orchestrator.api.deps import ApiContext
from ai_orchestrator.api.sources import download_task_answer
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import NotFoundError, PreconditionError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.security.auth import Principal

pytestmark = pytest.mark.integration


def context(tenant):
    actor = Actor(id="test:answer-download", kind=ActorType.HUMAN)
    return ApiContext(Principal(actor), tenant.session, tenant.organization_id, actor)


async def task(tenant, output=None, completed=True):
    repo = TaskRepository(tenant.session, tenant.organization_id)
    item = await repo.create(title="Synthetic answer export", goal="Export persisted answer only")
    if completed:
        await repo.transition(item.id, Transition.ASSIGN)
        await repo.transition(item.id, Transition.BEGIN_WORK)
        await repo.transition(item.id, Transition.COMPLETE)
    item.output = output or {}
    await tenant.session.flush()
    return item


async def test_download_preserves_full_unicode_and_structured_fields_without_reading_paths(tenant):
    summary = "Bản nháp HR, cần người duyệt. " * 100
    item = await task(tenant, {"summary": summary, "path": "/etc/passwd", "data": {"score": 0}})
    response = await download_task_answer(item.id, context(tenant))
    body = bytes(response.body)
    assert summary in body.decode()
    assert "/etc/passwd" in body.decode()
    assert '"score": 0' in body.decode()
    assert "root:x:" not in body.decode()
    assert response.headers["content-disposition"] == 'attachment; filename="task-answer.md"'
    assert response.headers["etag"] == '"' + hashlib.sha256(body).hexdigest() + '"'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("completed,output", [(False, {"summary": "unfinished"}), (True, {})])
async def test_download_rejects_unfinished_or_empty_answers(tenant, completed, output):
    item = await task(tenant, output, completed)
    with pytest.raises(PreconditionError):
        await download_task_answer(item.id, context(tenant))


async def test_download_cannot_read_another_tenant_answer(tenant, other_tenant):
    item = await task(other_tenant, {"summary": "Private answer"})
    item_id = item.id
    await other_tenant.commit()
    with pytest.raises(NotFoundError):
        await download_task_answer(item_id, context(tenant))
