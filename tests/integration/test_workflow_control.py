"""Independent sessions prove durable commands, feedback and SMTP outcome fencing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ai_orchestrator.application.business_workflow import BusinessWorkflowService
from ai_orchestrator.application.connector_actions import ConnectorActions, ConnectorUnknown
from ai_orchestrator.application.workflow_commands import command_for
from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
from ai_orchestrator.application.workflow_feedback import WorkflowFeedbackService, request_feedback
from ai_orchestrator.domain.contracts import Actor, AgentResult, AgentResultStatus
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.models import ConnectorAction, SkillVersion, Task
from tests.integration.test_business_workflow import FixtureRuntime, new_run
from tests.integration.test_business_workflow import prepared as business_prepared

prepared = business_prepared
pytestmark = pytest.mark.integration
BOSS = Actor(id="operator-feedback-test", kind=ActorType.HUMAN)


async def test_pending_generation_survives_shutdown_and_is_claimed_by_new_driver(prepared, db):
    org, root = prepared.organization_id, await new_run(prepared)
    entered = asyncio.Event()

    async def stopped(*_):
        entered.set()
        await asyncio.Event().wait()

    first = WorkflowDrivers(db, handler=stopped, durable=True)
    await first.submit(org, root, WorkflowKind.BUSINESS)
    await asyncio.wait_for(entered.wait(), 5)
    await first.shutdown()
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.settled_seq == 0 and command.requested_seq == 1
    calls = []

    async def resumed(*_):
        calls.append(root)

    second = WorkflowDrivers(db, handler=resumed, durable=True)
    await second.scan(organizations=[org])
    await second.wait(org, root)
    await second.shutdown()
    assert calls == [root]
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.settled_seq == command.requested_seq == 1


async def test_two_process_registries_cannot_acknowledge_another_owner_and_wakeup_is_retained(
    prepared, db
):
    org, root = prepared.organization_id, await new_run(prepared)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(*_):
        calls.append(root)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    first = WorkflowDrivers(db, handler=handler, durable=True)
    second = WorkflowDrivers(db, handler=handler, durable=True)
    await first.submit(org, root, WorkflowKind.BUSINESS)
    await asyncio.wait_for(entered.wait(), 5)
    second.start(org, root, WorkflowKind.BUSINESS, persist=False)
    await second.wait(org, root)
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.settled_seq == 0
    await first.submit(org, root, WorkflowKind.BUSINESS)
    release.set()
    await first.wait(org, root)
    await first.shutdown()
    await second.shutdown()
    assert len(calls) == 2
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.settled_seq == command.requested_seq == 2


class FeedbackRuntime:
    name = "workflow_evidence"

    def __init__(self, questions):
        self.questions = questions
        self.inputs = []

    async def execute(self, task, context, *, record_usage=None, **_):
        self.inputs.append(task.input)
        if record_usage:
            await record_usage(ModelResponse(provider="unit-fake", model_used="feedback-reference"))
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            task_id=task.task_id,
            execution_id=task.execution_id,
            summary="Feedback reviewed",
            output={
                "understanding": "Need more concrete material evidence",
                "questions": self.questions,
                "plan": [
                    {
                        "stage_key": "qualification",
                        "action": "Review supplier evidence",
                        "owner": "Procurement Agent",
                        "acceptance": "All three quotations and material approvals recorded",
                    }
                ],
                "skill_lesson": "Ask for missing evidence before recommending a supplier.",
            },
        )


async def test_feedback_reassessment_preserves_artifacts_and_proposes_unpublished_skill(
    prepared, db
):
    org, root = prepared.organization_id, await new_run(prepared, "live")
    report = await BusinessWorkflowService(db, org, runtime_factory=FixtureRuntime).run(root)
    original = {s["id"]: s["output"] for s in report["stages"] if s["status"] == "completed"}
    assert original and report["status"] == "blocked"
    async with db.tenant_session(org) as session:
        await request_feedback(
            session, org, root, "Please clarify material QA evidence and revise the plan", BOSS
        )
    service = WorkflowFeedbackService(
        db, org, runtime=FeedbackRuntime(["Which QA evidence is available?"])
    )
    await service.assess(root)
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.paused
        revision = command.feedback["revision"]
    with pytest.raises(ValidationError):
        await service.confirm(
            root, BOSS, revision=revision, answers="QA certificate included", new_revision=False
        )
    await service.answer(
        root,
        BOSS,
        revision=revision,
        answers="Use the signed QA certificate from the material specialist",
    )
    final_runtime = FeedbackRuntime([])
    final_service = WorkflowFeedbackService(db, org, runtime=final_runtime)
    await final_service.assess(root)
    assert final_runtime.inputs[0]["brief"]["answers"].startswith("Use the signed QA")
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command
        revision = command.feedback["revision"]
    with pytest.raises(PreconditionError):
        await final_service.confirm(
            root, BOSS, revision=revision - 1, answers="", new_revision=True
        )
    with pytest.raises(ValidationError, match="new corrections"):
        await final_service.confirm(
            root, BOSS, revision=revision, answers="Keep the quality gate", new_revision=True
        )
    destination, _ = await final_service.confirm(
        root, BOSS, revision=revision, answers="", new_revision=True
    )
    assert destination != root
    async with db.tenant_session(org) as session:
        command = await command_for(session, org, root)
        assert command and command.paused and command.feedback["state"] == "applied"
        skill = await session.get(SkillVersion, command.feedback["skill_version_id"])
        assert skill and not skill.is_published and skill.test_results == {}
        for task_id, output in original.items():
            task = await session.get(Task, task_id)
            assert task and task.output == output and task.status == "completed"
        successor = await session.get(Task, destination)
        assert successor and successor.input["revision_of"] == root
        assert successor.input["feedback_context"]["answers"].startswith("Use the signed QA")


class Mailbox:
    settings = SimpleNamespace(recruitment_mail_address="fixture@example.invalid")

    def __init__(self):
        self.sends = 0
        self.fail = False
        self.observed = False

    def send_prepared(self, root, cvs, ids):
        self.sends += 1
        if self.fail:
            raise TimeoutError("SMTP reply lost after write")
        return {
            "sent": [{"message_id": ids[0], "sha256": "recorded"}],
            "count": 1,
            "synthetic": True,
            "protocol": "SMTP_SSL",
        }

    def reconcile_tests(self, root, snapshot):
        return {
            "confirmed": self.observed,
            "observed": snapshot["messages"] if self.observed else [],
            "receipt": {
                "sent": snapshot["messages"],
                "count": 1,
                "synthetic": True,
                "readonly": True,
                "protocol": "fixture-read-back",
            },
        }


async def action_stage(db, org, root):
    async with db.tenant_session(org) as session:
        return await session.scalar(
            select(Task.id)
            .where(Task.organization_id == org, Task.parent_task_id == root)
            .order_by(Task.created_at)
            .limit(1)
        )


async def test_uncertain_smtp_is_never_resent_missing_readback_does_not_claim_failure(prepared, db):
    org, root = prepared.organization_id, await new_run(prepared)
    stage = await action_stage(db, org, root)
    mailbox = Mailbox()
    mailbox.fail = True
    service = ConnectorActions(db, org)
    cvs = [{"name": "Synthetic", "filename": "cv.txt", "text": "MEP commissioning evidence"}]
    with pytest.raises(ConnectorUnknown):
        await service.send_tests(root, stage, mailbox, cvs)
    async with db.tenant_session(org) as session:
        action = await session.scalar(
            select(ConnectorAction).where(ConnectorAction.root_task_id == root)
        )
        assert action and action.state == "unknown"
        action_id = action.id
    mailbox.fail = False
    with pytest.raises(ConnectorUnknown):
        await service.send_tests(root, stage, mailbox, cvs)
    missing = await service.reconcile(action_id, mailbox, BOSS)
    assert missing["state"] == "unknown" and mailbox.sends == 1
    mailbox.observed = True
    confirmed = await service.reconcile(action_id, mailbox, BOSS)
    assert confirmed["state"] == "confirmed"
    reused = await service.send_tests(root, stage, mailbox, cvs)
    assert reused["reused_receipt"] and mailbox.sends == 1
    with pytest.raises(PreconditionError):
        await service.send_tests(root, stage, mailbox, [{**cvs[0], "text": "changed"}])


async def test_commands_and_receipts_are_invisible_to_other_tenants(prepared, db):
    org, root = prepared.organization_id, await new_run(prepared)
    driver = WorkflowDrivers(db, handler=lambda *_: asyncio.sleep(0), durable=True)
    await driver.submit(org, root, WorkflowKind.BUSINESS)
    await driver.wait(org, root)
    await driver.shutdown()
    stage = await action_stage(db, org, root)
    await ConnectorActions(db, org).send_tests(
        root, stage, Mailbox(), [{"name": "Synthetic", "filename": "cv.txt", "text": "MEP source"}]
    )
    async with db.tenant_session(make_id("org")) as session:
        assert await command_for(session, org, root) is None
        assert (await session.scalars(select(ConnectorAction))).all() == []


@pytest.mark.parametrize("mode", ["dispatch", "before_write", "after_write"])
async def test_abrupt_process_death_retains_command_or_uncertain_connector_intent(
    prepared, db, tmp_path, mode
):
    import json
    import sys

    org, root = prepared.organization_id, await new_run(prepared)
    stage = await action_stage(db, org, root)
    ready, effect = tmp_path / "ready.json", tmp_path / "effect.txt"
    log = tmp_path / "child.log"
    with log.open("wb") as output:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.integration.control_crash_process",
            mode,
            org,
            root,
            stage,
            str(ready),
            str(effect),
            stdout=output,
            stderr=output,
        )
        try:
            async with asyncio.timeout(15):
                while not ready.exists():
                    assert child.returncode is None, log.read_text()
                    await asyncio.sleep(0.05)
            assert json.loads(ready.read_text())["root"] == root
            child.kill()
            await asyncio.wait_for(child.wait(), 5)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    if mode == "dispatch":
        async with db.tenant_session(org) as session:
            command = await command_for(session, org, root)
            assert command and command.requested_seq == 1 and command.settled_seq == 0
        calls = []

        async def recovered(*_):
            calls.append(root)

        driver = WorkflowDrivers(db, handler=recovered, durable=True)
        await driver.scan(organizations=[org])
        await driver.wait(org, root)
        await driver.shutdown()
        assert calls == [root]
    else:
        async with db.tenant_session(org) as session:
            action = await session.scalar(
                select(ConnectorAction).where(ConnectorAction.root_task_id == root)
            )
            assert action and action.state == "sending"
            action_id = action.id

        class ReadBack:
            def reconcile_tests(self, run, snapshot):
                observed = effect.exists()
                return {
                    "confirmed": observed,
                    "observed": snapshot["messages"] if observed else [],
                    "receipt": {
                        "sent": snapshot["messages"],
                        "count": 1,
                        "protocol": "fixture-filesystem-readback",
                        "synthetic": True,
                    },
                }

        result = await ConnectorActions(db, org).reconcile(action_id, ReadBack(), BOSS)
        assert result["state"] == ("confirmed" if mode == "after_write" else "sending")
        assert effect.exists() == (mode == "after_write")
