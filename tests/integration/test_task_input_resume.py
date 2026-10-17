"""Human clarification queues ordinary tasks without overwriting source inputs."""

import json
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError as BodyValidationError
from sqlalchemy import select

from ai_orchestrator.api.deps import ApiContext
from ai_orchestrator.api.tasks import TaskInputRequest, get_task, provide_task_inputs
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PreconditionError,
    ValidationError,
)
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import AuditLog, Execution
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.security.auth import Principal

pytestmark = pytest.mark.integration


def context(tenant, kind=ActorType.HUMAN):
    actor = Actor(id="test:human", kind=kind)
    return ApiContext(Principal(actor), tenant.session, tenant.organization_id, actor)


def request(key=None):
    headers = [(b"idempotency-key", key.encode())] if key else []
    return Request({"type": "http", "headers": headers})


async def waiting(tenant, **kwargs):
    repo = TaskRepository(tenant.session, tenant.organization_id)
    task = await repo.create(title="Clarify", goal=str(uuid4()), **kwargs)
    await repo.transition(task.id, Transition.ASSIGN)
    await repo.transition(task.id, Transition.BEGIN_WORK)
    await repo.transition(task.id, Transition.REQUEST_INPUT)
    return task


async def test_clarification_preserves_sources_and_records_one_private_audit(tenant):
    original = {
        "sources": [{"id": "source1", "text": "Untrusted supplied document"}],
        "config": {"read_only": True},
    }
    task = await waiting(tenant, input=original)
    key = str(uuid4())
    body = TaskInputRequest(clarification="Use the signed October version")
    result = await provide_task_inputs(task.id, body, request(key), context(tenant))
    assert result["status"] == "assigned"
    assert result["input"]["sources"] == original["sources"]
    assert result["input"]["config"] == original["config"]
    assert result["input"]["clarifications"] == [
        {"text": body.clarification, "actor_id": "test:human", "source": "human_clarification"}
    ]
    assert (
        await tenant.session.scalar(select(Execution.id).where(Execution.task_id == task.id))
        is None
    )
    replay = await provide_task_inputs(task.id, body, request(key), context(tenant))
    assert replay["idempotent_replay"] is True
    assert len(replay["input"]["clarifications"]) == 1
    audit = (
        await tenant.session.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == task.id, AuditLog.action == "task.input_provided"
            )
        )
    ).all()
    assert len(audit) == 1
    assert body.clarification not in json.dumps(audit[0].context)
    with pytest.raises(ConflictError):
        await provide_task_inputs(
            task.id, TaskInputRequest(clarification="Different"), request(key), context(tenant)
        )


@pytest.mark.parametrize("kind", [ActorType.AGENT, ActorType.SERVICE])
async def test_nonhuman_cannot_supply_clarification(tenant, kind):
    task = await waiting(tenant)
    with pytest.raises(AuthorizationError):
        await provide_task_inputs(
            task.id,
            TaskInputRequest(clarification="answer"),
            request(str(uuid4())),
            context(tenant, kind),
        )


@pytest.mark.parametrize(
    "marker",
    [
        "source_workflow",
        "business_workflow",
        "agent_workflow",
        "workflow_revision_for",
        "workflow_feedback_for",
        "agent_blueprint_draft",
    ],
)
async def test_dedicated_workflow_gates_cannot_be_bypassed(tenant, marker):
    task = await waiting(tenant, input={marker: "controlled"})
    with pytest.raises(PreconditionError, match="dedicated"):
        await provide_task_inputs(
            task.id,
            TaskInputRequest(clarification="answer"),
            request(str(uuid4())),
            context(tenant),
        )
    assert task.status == "waiting_for_input"


async def test_terminal_ancestor_blocks_resume(tenant):
    repo = TaskRepository(tenant.session, tenant.organization_id)
    parent = await repo.create(title="parent", goal=str(uuid4()))
    child = await waiting(tenant, parent_task_id=parent.id)
    await repo.transition(parent.id, Transition.CANCEL)
    with pytest.raises(PreconditionError, match="terminal ancestor"):
        await provide_task_inputs(
            child.id,
            TaskInputRequest(clarification="answer"),
            request(str(uuid4())),
            context(tenant),
        )


async def test_active_execution_blocks_resume(tenant):
    task = await waiting(tenant)
    tenant.session.add(
        Execution(
            id="exe_" + uuid4().hex[:26],
            organization_id=tenant.organization_id,
            task_id=task.id,
            status="running",
        )
    )
    await tenant.session.flush()
    with pytest.raises(PreconditionError, match="active execution"):
        await provide_task_inputs(
            task.id,
            TaskInputRequest(clarification="answer"),
            request(str(uuid4())),
            context(tenant),
        )


async def test_wrong_state_and_missing_idempotency_are_rejected(tenant):
    task = await waiting(tenant)
    body = TaskInputRequest(clarification="answer")
    with pytest.raises(ValidationError):
        await provide_task_inputs(task.id, body, request(), context(tenant))
    await TaskRepository(tenant.session, tenant.organization_id).transition(
        task.id, Transition.CANCEL
    )
    with pytest.raises(PreconditionError):
        await provide_task_inputs(task.id, body, request(str(uuid4())), context(tenant))


async def test_other_tenant_task_is_not_visible(tenant):
    task = await waiting(tenant)
    task_id = task.id
    await tenant.commit()
    other_org = "org_" + uuid4().hex[:26]
    async with tenant.db.tenant_session(other_org) as session:
        actor = Actor(id="test:other-human", kind=ActorType.HUMAN)
        ctx = ApiContext(Principal(actor), session, other_org, actor)
        with pytest.raises(NotFoundError):
            await provide_task_inputs(
                task_id,
                TaskInputRequest(clarification="answer"),
                request(str(uuid4())),
                ctx,
            )


@pytest.mark.parametrize(
    "payload",
    [
        {"clarification": " "},
        {"clarification": "x" * 20001},
        {"clarification": "answer", "status": "completed"},
        {"clarification": "answer", "sources": []},
    ],
)
def test_input_contract_rejects_blank_oversize_and_privileged_overwrites(payload):
    with pytest.raises(BodyValidationError):
        TaskInputRequest.model_validate(payload)


async def test_active_lease_blocks_input_without_execution(tenant):
    from datetime import UTC, datetime, timedelta

    task = await waiting(tenant)
    task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await tenant.session.flush()
    with pytest.raises(PreconditionError, match="active execution or lease"):
        await provide_task_inputs(
            task.id,
            TaskInputRequest(clarification="answer"),
            request(str(uuid4())),
            context(tenant),
        )


async def test_same_idempotency_key_cannot_target_another_task(tenant):
    first = await waiting(tenant)
    second = await waiting(tenant)
    key = str(uuid4())
    body = TaskInputRequest(clarification="answer")
    await provide_task_inputs(first.id, body, request(key), context(tenant))
    with pytest.raises(ConflictError):
        await provide_task_inputs(second.id, body, request(key), context(tenant))
    assert second.status == "waiting_for_input"


async def test_second_clarification_preserves_existing_history(tenant):
    task = await waiting(tenant)
    await provide_task_inputs(
        task.id, TaskInputRequest(clarification="first"), request(str(uuid4())), context(tenant)
    )
    repo = TaskRepository(tenant.session, tenant.organization_id)
    await repo.transition(task.id, Transition.BEGIN_WORK)
    await repo.transition(task.id, Transition.REQUEST_INPUT)
    response = await provide_task_inputs(
        task.id, TaskInputRequest(clarification="second"), request(str(uuid4())), context(tenant)
    )
    assert [row["text"] for row in response["input"]["clarifications"]] == ["first", "second"]


async def test_real_runner_pauses_with_question_and_completes_after_clarification(tenant):
    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
    from ai_orchestrator.application.task_attempt import TaskAttemptRunner
    from ai_orchestrator.models.gateway import ModelResponse
    from ai_orchestrator.persistence.models import Agent, Organization
    from ai_orchestrator.seed import seed

    class ClarifyingRuntime(ScriptedRuntime):
        name = "clarification-offline-test"

        async def execute(self, task, context, **kwargs):
            clarifications = context.task.input.get("clarifications", [])
            answer = clarifications[-1]["text"] if clarifications else None

            class Gateway:
                async def complete(self, request):
                    return ModelResponse(
                        text=json.dumps({"selected_version": answer})
                        if answer
                        else json.dumps(
                            {
                                "request_input": {
                                    "question": "Which signed source version should be used?"
                                }
                            }
                        ),
                        model_used="scripted-pydanticai-test",
                    )

            return await run_with_pydantic_ai(task, context, gateway=Gateway())

    organization = await tenant.session.scalar(
        select(Organization).where(Organization.id == tenant.organization_id)
    )
    await seed(tenant.session, into=organization)
    agent = await tenant.session.scalar(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id, Agent.name == "Finance Agent"
        )
    )
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="Select signed source",
        goal="Clarify the source version then prepare a result",
        task_type="analysis",
        owner_agent_id=agent.id,
        input={"sources": [{"id": "draft", "text": "Unsigned draft reference"}]},
        expected_output_schema={
            "type": "object",
            "properties": {"selected_version": {"type": "string"}},
            "required": ["selected_version"],
        },
    )
    task_id = task.id
    await tenant.commit()
    runner = TaskAttemptRunner(
        tenant.db, tenant.organization_id, runtime=ClarifyingRuntime(), auto_approve=False
    )
    paused = await runner.execute_task(task_id)
    assert paused.status.value == "waiting_for_input"
    tenant.session.expire_all()
    current = await TaskRepository(tenant.session, tenant.organization_id).get(task_id)
    assert current.last_error == "Which signed source version should be used?"
    detail = await get_task(task_id, context(tenant))
    assert detail["last_error"] == current.last_error
    response = await provide_task_inputs(
        task_id,
        TaskInputRequest(clarification="signed October"),
        request(str(uuid4())),
        context(tenant),
    )
    assert response["last_error"] is None
    assert response["status"] == "assigned"
    assert response["input"]["sources"][0]["id"] == "draft"
    await tenant.commit()
    completed = await runner.execute_task(task_id)
    assert completed.status.value == "completed"
    async with tenant.db.tenant_session(tenant.organization_id) as verification:
        current = await TaskRepository(verification, tenant.organization_id).get(task_id)
        assert current.output == {"selected_version": "signed October"}


async def test_nonpydantic_runtime_cannot_complete_with_fabricated_source_quote(tenant):
    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.application.task_execution import TaskExecutionService
    from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus
    from ai_orchestrator.persistence.models import Agent, Organization
    from ai_orchestrator.seed import seed

    class ForgedQuoteRuntime(ScriptedRuntime):
        name = "forged-quote-offline-test"

        async def execute(self, task, context, **kwargs):
            return AgentResult(
                status=AgentResultStatus.COMPLETED,
                summary="Claimed source review",
                output={
                    "findings": [
                        {"source_id": "policy", "evidence_quote": "[policy] Signed policy text."}
                    ]
                },
                task_id=str(task.task_id),
                execution_id=str(task.execution_id),
            )

    organization = await tenant.session.scalar(
        select(Organization).where(Organization.id == tenant.organization_id)
    )
    await seed(tenant.session, into=organization)
    agent = await tenant.session.scalar(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id, Agent.name == "Finance Agent"
        )
    )
    schema = {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "evidence_quote": {"type": "string"},
                    },
                },
            }
        },
    }
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="Review evidence",
        goal="Check source quotes",
        task_type="analysis",
        owner_agent_id=agent.id,
        input={"sources": [{"id": "policy", "text": "Signed policy text."}]},
        expected_output_schema=schema,
    )
    result = await TaskExecutionService(
        tenant.session, tenant.organization_id, runtime=ForgedQuoteRuntime()
    ).execute_task(task.id)
    assert result.status.value == "failed"
    assert task.failure_category == "output_contract_unmet"
    assert "Source evidence contract failed" in task.last_error


@pytest.mark.parametrize("length", [200, 201])
async def test_idempotency_key_length_is_validated_before_storage(tenant, length):
    task = await waiting(tenant)
    body = TaskInputRequest(clarification="answer")
    key = "k" * length
    if length == 201:
        with pytest.raises(ValidationError):
            await provide_task_inputs(task.id, body, request(key), context(tenant))
    else:
        await provide_task_inputs(task.id, body, request(key), context(tenant))
        replay = await provide_task_inputs(task.id, body, request(key), context(tenant))
        assert replay["idempotent_replay"] is True


async def test_same_client_key_replays_independently_in_two_tenants(tenant, admin_db):
    from types import SimpleNamespace

    from tests.integration.tenant_context import _create_organization

    key = str(uuid4())
    body = TaskInputRequest(clarification="answer")
    first = await waiting(tenant)
    first_id = first.id
    await provide_task_inputs(first_id, body, request(key), context(tenant))
    await tenant.commit()
    other_org = await _create_organization(admin_db, "clarification-key")
    async with tenant.db.tenant_session(other_org) as session:
        other = SimpleNamespace(session=session, organization_id=other_org)
        task = await waiting(other)
        task_id = task.id
        await provide_task_inputs(task_id, body, request(key), context(other))
    async with tenant.db.tenant_session(other_org) as session:
        other = SimpleNamespace(session=session, organization_id=other_org)
        replay = await provide_task_inputs(task_id, body, request(key), context(other))
        assert replay["idempotent_replay"] is True
        assert replay["id"] == task_id
    first_replay = await provide_task_inputs(first_id, body, request(key), context(tenant))
    assert first_replay["idempotent_replay"] is True
    assert first_replay["id"] == first_id


async def test_actual_pydanticai_schemaless_prose_persists_on_task(tenant):
    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
    from ai_orchestrator.application.task_attempt import TaskAttemptRunner
    from ai_orchestrator.models.gateway import ModelResponse
    from ai_orchestrator.persistence.models import Agent, Organization
    from ai_orchestrator.seed import seed

    class Gateway:
        async def complete(self, request):
            return ModelResponse(
                text="HR onboarding guide: 30/60/90 days.", model_used="scripted-pydanticai-test"
            )

    class Runtime(ScriptedRuntime):
        async def execute(self, task, context, **kwargs):
            return await run_with_pydantic_ai(task, context, gateway=Gateway())

    organization = await tenant.session.scalar(
        select(Organization).where(Organization.id == tenant.organization_id)
    )
    await seed(tenant.session, into=organization)
    agent = await tenant.session.scalar(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id, Agent.name == "HR Agent"
        )
    )
    task = await TaskRepository(tenant.session, tenant.organization_id).create(
        title="HR custom guide",
        goal="Prepare onboarding policy guide without personal data",
        task_type="analysis",
        owner_agent_id=agent.id,
    )
    task_id = task.id
    await tenant.commit()
    result = await TaskAttemptRunner(
        tenant.db, tenant.organization_id, runtime=Runtime(), auto_approve=False
    ).execute_task(task_id)
    assert result.status.value == "completed"
    async with tenant.db.tenant_session(tenant.organization_id) as verification:
        stored = await TaskRepository(verification, tenant.organization_id).get(task_id)
        assert stored.output == {"summary": "HR onboarding guide: 30/60/90 days."}


async def test_ordinary_hr_retry_retains_owner_clarification_contract_and_history(tenant):
    import datetime as dt

    from sqlalchemy import text

    from ai_orchestrator.agent_runtime import ScriptedRuntime
    from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
    from ai_orchestrator.application.task_attempt import TaskAttemptRunner
    from ai_orchestrator.application.task_retry import retry_failed
    from ai_orchestrator.models.gateway import ModelResponse
    from ai_orchestrator.persistence.models import Agent, Organization
    from ai_orchestrator.seed import seed

    organization = await tenant.session.scalar(
        select(Organization).where(Organization.id == tenant.organization_id)
    )
    await seed(tenant.session, into=organization)
    owner = await tenant.session.scalar(
        select(Agent).where(
            Agent.organization_id == tenant.organization_id, Agent.name == "HR Agent"
        )
    )
    owner_id = owner.id
    original_input = {
        "clarifications": [
            {
                "text": "Procurement specialist",
                "source": "human_clarification",
                "actor_id": "test:human",
            }
        ]
    }
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    repo = TaskRepository(tenant.session, tenant.organization_id)
    old = await repo.create(
        title="HR onboarding",
        goal="Produce HR onboarding draft after clarification",
        task_type="analysis",
        owner_agent_id=owner_id,
        input=original_input,
        expected_output_schema=schema,
    )
    old_id = old.id
    await repo.transition(old_id, Transition.BEGIN_WORK)
    await repo.transition(old_id, Transition.FAIL)
    new = await retry_failed(
        tenant.session,
        organization_id=tenant.organization_id,
        task_id=old_id,
        now=dt.datetime.now(dt.UTC),
    )
    retried = await repo.get(new["task_id"])
    assert retried.owner_agent_id == owner_id
    assert retried.input == original_input
    assert retried.expected_output_schema == schema
    assert retried.status == "assigned"
    dependency = await tenant.session.scalar(
        text(
            "SELECT count(*) FROM task_dependencies "
            "WHERE organization_id=:org AND task_id=:new AND depends_on_task_id=:old"
        ),
        {"org": tenant.organization_id, "new": retried.id, "old": old_id},
    )
    assert dependency == 1
    await tenant.commit()

    class Gateway:
        async def complete(self, request):
            assert "Procurement specialist" in request.prompt
            return ModelResponse(
                text='{"result":"HR onboarding draft"}', model_used="scripted-pydanticai-test"
            )

    class Runtime(ScriptedRuntime):
        async def execute(self, task, context, **kwargs):
            assert str(context.actor.id) == owner_id
            return await run_with_pydantic_ai(task, context, gateway=Gateway())

    result = await TaskAttemptRunner(
        tenant.db, tenant.organization_id, runtime=Runtime(), auto_approve=False
    ).execute_task(new["task_id"])
    assert result.status.value == "completed"
    async with tenant.db.tenant_session(tenant.organization_id) as verification:
        stored = TaskRepository(verification, tenant.organization_id)
        assert (await stored.get(old_id)).status == "failed"
        current = await stored.get(new["task_id"])
        assert current.owner_agent_id == owner_id
        assert current.output == {"result": "HR onboarding draft"}
