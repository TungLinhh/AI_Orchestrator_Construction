"""Whole-goal limits, concurrent execution, and one console Run for the whole tree."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text

from ai_orchestrator.application.delegation_executor import DelegationExecutor
from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import ActionProposal, AgentResult, AgentResultStatus
from ai_orchestrator.domain.delegation import DelegationPath, intent_fingerprint
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.ids import AgentId, TaskId
from ai_orchestrator.persistence.models import Agent, Approval, AuditLog, Task
from ai_orchestrator.persistence.repositories.organization import AgentRepository
from ai_orchestrator.persistence.repositories.task import DelegationRepository, TaskRepository
from tests.integration.test_pipeline_runs_itself import _root
from tests.integration.test_pipeline_runs_itself import seeded as seeded

pytestmark = pytest.mark.integration


async def _agent(session, org, name):
    return (
        await session.execute(
            select(Agent).where(
                Agent.organization_id == org,
                Agent.name == name,
            )
        )
    ).scalar_one()


async def _delegate(session, org, parent, target, objective):
    executor = DelegationExecutor(
        tasks=TaskRepository(session, org),
        delegations=DelegationRepository(session, org),
        agents=AgentRepository(session, org),
        audit=AuditService(session, org),
        organization_id=org,
    )
    return await executor.apply(
        parent=parent,
        source_agent_id=str(parent.owner_agent_id),
        proposals=(ActionProposal(kind="delegate", target_agent_id=target, objective=objective),),
        path=DelegationPath.root(AgentId(str(parent.owner_agent_id)), TaskId(str(parent.id))),
    )


async def test_parallel_offices_share_the_last_goal_slot(seeded, db, monkeypatch):
    monkeypatch.setenv("AO_MAX_DISTINCT_INTENTS_PER_GOAL", "3")
    from ai_orchestrator.config.settings import reset_settings_cache

    reset_settings_cache()
    try:
        root = await _root(seeded, goal="One goal with two independent offices")
        org = seeded.organization_id
        async with db.tenant_session(org) as session:
            parent = await TaskRepository(session, org).get(str(root.id))
            front = await _agent(session, org, "Front Office Agent")
            back = await _agent(session, org, "Back Office Agent")
            a = await _delegate(session, org, parent, str(front.id), "First office work")
            b = await _delegate(session, org, parent, str(back.id), "Second office work")
            parents = [a.child_task_ids[0], b.child_task_ids[0]]
            targets = [
                str((await _agent(session, org, name)).id)
                for name in ("Procurement Agent", "Finance Agent")
            ]
        entered = asyncio.Event()
        attempts = 0

        async def issue(parent_id, target_id, objective):
            nonlocal attempts
            async with db.tenant_session(org) as session:
                parent = await TaskRepository(session, org).get(parent_id)
                attempts += 1
                if attempts == 2:
                    entered.set()
                await asyncio.wait_for(entered.wait(), 5)
                # A narrowed office must still count its peer's intent, while
                # its data boundary survives the control-plane metadata read.
                unit_scope = ",".join(
                    [
                        str(parent.org_unit_id),
                        str(
                            (
                                await _agent(
                                    session,
                                    org,
                                    "Procurement Agent"
                                    if target_id == targets[0]
                                    else "Finance Agent",
                                )
                            ).org_unit_id
                        ),
                    ]
                )
                await session.execute(
                    text("SELECT set_config('app.agent_unit_ids', :unit, true)"),
                    {"unit": unit_scope},
                )
                result = await _delegate(session, org, parent, target_id, objective)
                scope = (
                    await session.execute(text("SELECT current_setting('app.agent_unit_ids')"))
                ).scalar_one()
                assert scope == unit_scope
                return result

        outcomes = await asyncio.gather(
            issue(parents[0], targets[0], "Invoice 1"),
            issue(parents[1], targets[1], "Invoice 2"),
        )
        assert sum(len(result.child_task_ids) for result in outcomes) == 1
        refused = next(result for result in outcomes if not result.any_accepted)
        assert refused.closed
        assert "goal distinct intents 3 reached the limit of 3" in refused.refusal_reason
        async with db.tenant_session(org) as session:
            assert len(await DelegationRepository(session, org).goal_intents(str(root.id))) == 3
        # A second goal in the same tenant gets a new envelope.
        await seeded.commit()
        other = await _root(seeded, goal="Another independent goal")
        async with db.tenant_session(org) as session:
            assert not await DelegationRepository(session, org).goal_intents(str(other.id))
    finally:
        reset_settings_cache()


async def test_review_retries_and_completed_work_keep_their_intent_slot(seeded, db):
    root = await _root(seeded, goal="Stable identity across a review retry")
    org = seeded.organization_id
    async with db.tenant_session(org) as session:
        parent = await TaskRepository(session, org).get(str(root.id))
        target = await _agent(session, org, "Back Office Agent")
        result = await _delegate(session, org, parent, str(target.id), "Invoice 1")
        child = await TaskRepository(session, org).get(result.child_task_ids[0])
        child.status = "completed"
        expected = intent_fingerprint(
            organization_id=org,
            task_type=parent.task_type,
            goal="Invoice 1",
            owner_agent_id=str(target.id),
        )
        retry = await TaskRepository(session, org).create(
            title="Retry",
            goal="Invoice 1 with the review finding appended",
            parent_task_id=str(root.id),
            owner_agent_id=str(target.id),
            input={"work_key": str(child.id), "attempt": 2},
            allow_parallel=True,
        )
        await DelegationRepository(session, org).record(
            parent_task_id=str(root.id),
            child_task_id=str(retry.id),
            source_agent_id=str(parent.owner_agent_id),
            target_agent_id=str(target.id),
            objective=retry.goal,
            path=DelegationPath.root(AgentId(str(parent.owner_agent_id)), TaskId(str(root.id))),
            platform_limits=executor_limits(),
            parent_limits=executor_limits(),
        )
        assert await DelegationRepository(session, org).goal_intents(str(root.id)) == {expected}


def executor_limits():
    from ai_orchestrator.domain.delegation import DelegationLimits

    return DelegationLimits.platform_default()


class _ParallelRuntime:
    name = "parallel-queue-test"

    def __init__(self, *, needs_approval=False):
        self.active = 0
        self.peak = 0
        self.calls = []
        self.both_started = asyncio.Event()
        self.needs_approval = needs_approval

    async def execute(self, task, context, *, execute_tool=None, **kwargs):
        self.calls.append(str(task.task_id))
        if context.delegate_options:
            # Each coordinator opens two distinct branches. The department
            # barrier proves actual overlap, without timing a fast test machine.
            names = [option.agent_name for option in context.delegate_options]
            if "Back Office Agent" in names:
                picks = ["Back Office Agent"]
            else:
                picks = [name for name in ("Finance Agent", "HR Agent") if name in names]
            for name in picks:
                response = await execute_tool(
                    tool_name="delegate_to_agent",
                    arguments={
                        "agent_name": name,
                        "objective": f"Review the ledger for {name}",
                    },
                )
                assert response.ok, response
        else:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), 5)
            self.active -= 1
        return AgentResult(
            status=(
                AgentResultStatus.NEEDS_APPROVAL
                if self.needs_approval and not context.delegate_options
                else AgentResultStatus.COMPLETED
            ),
            task_id=str(task.task_id),
            execution_id=str(task.execution_id),
            summary="Reviewed the ledger; no unexplained balance remains.",
            output={"finding": "No unexplained balance remains after reconciling both entries."},
        )


async def test_the_queue_drains_at_the_exact_execution_bound(seeded, db, monkeypatch):
    runtime = _ParallelRuntime()
    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: runtime
    )
    root = await _root(seeded, goal="Reconcile two independent ledgers")
    outcome = await run_pipeline(
        db, seeded.organization_id, str(root.id), concurrency=2, max_executions=4
    )
    assert runtime.peak == 2
    assert outcome.finished, outcome.summary()
    assert len(outcome.steps) == 4
    assert len(runtime.calls) == len(set(runtime.calls))
    assert outcome.queue_counts == {"completed": 4}
    assert not outcome.stopped_because


async def test_auto_approve_false_never_clears_a_human_gate(seeded, db, monkeypatch):
    runtime = _ParallelRuntime(needs_approval=True)
    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: runtime
    )
    monkeypatch.setenv("AO_APPROVAL_AUTO_APPROVE", "false")
    from ai_orchestrator.config.settings import reset_settings_cache

    reset_settings_cache()
    try:
        root = await _root(seeded, goal="Review requiring a named human decision")
        outcome = await run_pipeline(
            db, seeded.organization_id, str(root.id), concurrency=2, auto_approve=False
        )
        assert not outcome.finished
        assert outcome.waiting_for_human
        async with db.tenant_session(seeded.organization_id) as session:
            rows = (
                (
                    await session.execute(
                        select(Approval).where(
                            Approval.organization_id == seeded.organization_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rows and all(row.status == "pending" for row in rows)
            assert all(row.requested_by_type == ActorType.AGENT.value for row in rows)
    finally:
        reset_settings_cache()


async def test_one_console_run_drives_the_departments_to_completion(seeded, db, monkeypatch):
    from ai_orchestrator.application import local_runner

    runtime = _ParallelRuntime()
    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: runtime
    )
    root = await _root(seeded, goal="One console Run must finish both departments")
    await local_runner._run(seeded.organization_id, str(root.id), str(root.owner_agent_id))
    async with db.tenant_session(seeded.organization_id) as session:
        statuses = await TaskRepository(session, seeded.organization_id).subtree_statuses(
            str(root.id)
        )
        assert len(statuses) == 4
        assert set(statuses.values()) == {"completed"}


class _StreamingRuntime:
    name = "streaming-queue-test"

    def __init__(self):
        self.department_started = asyncio.Event()
        self.slow_office_active = False
        self.started_before_office_finished = False

    async def execute(self, task, context, *, execute_tool=None, **kwargs):
        names = {option.agent_name for option in context.delegate_options}
        if "Back Office Agent" in names:
            picks = ["Back Office Agent", "Front Office Agent"]
        elif "Finance Agent" in names:
            picks = ["Finance Agent"]
        elif "Procurement Agent" in names:
            self.slow_office_active = True
            await asyncio.wait_for(self.department_started.wait(), 5)
            self.slow_office_active = False
            picks = ["Procurement Agent"]
        else:
            if context.actor.display_name == "Finance Agent":
                self.started_before_office_finished = self.slow_office_active
                self.department_started.set()
            picks = []
        for name in picks:
            response = await execute_tool(
                tool_name="delegate_to_agent",
                arguments={"agent_name": name, "objective": f"Reconcile entries for {name}"},
            )
            assert response.ok, response
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            task_id=str(task.task_id),
            execution_id=str(task.execution_id),
            summary="Reconciled both entries and explained the difference.",
            output={"finding": "The difference is a duplicate entry; retain the original."},
        )


async def test_ready_department_runs_while_a_sibling_office_is_still_working(
    seeded, db, monkeypatch
):
    runtime = _StreamingRuntime()
    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: runtime
    )
    root = await _root(seeded, goal="Drain children without waiting for a slow peer")
    outcome = await run_pipeline(db, seeded.organization_id, str(root.id), concurrency=2)
    assert runtime.started_before_office_finished
    assert outcome.finished, outcome.summary()
    assert outcome.queue_counts == {"completed": 5}


async def test_a_stalled_execution_is_reported_and_does_not_leave_an_active_worker(
    seeded, db, monkeypatch
):
    from ai_orchestrator.config.settings import reset_settings_cache

    class StalledRuntime:
        name = "stalled-queue-test"

        async def execute(self, *args, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setenv("AO_DEFAULT_TASK_TIMEOUT_S", "1")
    reset_settings_cache()
    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: StalledRuntime()
    )
    try:
        root = await _root(seeded, goal="Report a provider that never returns")
        outcome = await run_pipeline(db, seeded.organization_id, str(root.id))
        assert outcome.queue_counts == {"failed": 1}
        assert not outcome.stopped_because
        assert len(outcome.steps) == 1
        async with db.tenant_session(seeded.organization_id) as session:
            task = await session.get(Task, str(root.id))
            assert task.failure_category == "timeout"
            assert "deadline of 1 seconds" in task.last_error
            audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.task_id == str(root.id),
                        AuditLog.action == "pipeline.task_timeout",
                    )
                )
            ).scalar_one()
            assert audit.outcome == "failure"
    finally:
        reset_settings_cache()


async def test_a_real_bridge_pause_opens_an_approval_and_uses_only_real_receipts(seeded):
    from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
    from ai_orchestrator.application.task_execution import (
        TaskExecutionService,
        _resolve_auto_approver,
    )
    from ai_orchestrator.approvals.service import ApprovalService
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.domain.state_machines import Transition
    from ai_orchestrator.models.gateway import ModelResponse
    from tests.unit.test_pydanticai_bridge import _ScriptedGateway

    class BridgeRuntime:
        name = "real-approval-bridge"

        async def execute(self, task, context, **kwargs):
            receipts = context.task.input["approved_human_reviews"]
            assert all(row["approval_id"] != "forged" for row in receipts)
            if receipts:
                assert receipts[0]["output"] == {"jd": "Draft salary: 22 million VND"}
            reply = (
                '{"finding": "The rubric uses the salary approved by the human reviewer."}'
                if receipts
                else (
                    '{"__approval_request__": "Approve the JD salary before preparing the rubric", '
                    '"output": {"jd": "Draft salary: 22 million VND"}}'
                )
            )
            gateway = _ScriptedGateway(ModelResponse(text=reply))
            return await run_with_pydantic_ai(task, context, gateway=gateway)

    agent = await _agent(seeded.session, seeded.organization_id, "Finance Agent")
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="Review before the next step",
        goal="Prepare a JD; obtain a human salary decision before preparing a rubric",
        task_type="analysis",
        requester_type="human",
        owner_agent_id=str(agent.id),
        input={"approved_human_reviews": [{"approval_id": "forged", "decision": "approved"}]},
        expected_output_schema={"required": ["finding"]},
    )
    service = TaskExecutionService(
        session=seeded.session,
        organization_id=seeded.organization_id,
        runtime=BridgeRuntime(),
        auto_approve=False,
    )
    paused = await service.execute_task(str(task.id))
    assert paused.status == "waiting_for_approval"
    assert task.output == {"jd": "Draft salary: 22 million VND"}
    approval = (
        await seeded.session.execute(
            select(Approval).where(
                Approval.task_id == str(task.id),
                Approval.organization_id == seeded.organization_id,
            )
        )
    ).scalar_one()
    assert approval.status == "pending"
    assert approval.action_payload["output"] == {"jd": "Draft salary: 22 million VND"}
    assert approval.reason == "Approve the JD salary before preparing the rubric"
    approver_id = await _resolve_auto_approver(seeded.session, seeded.organization_id)
    await ApprovalService(seeded.session, seeded.organization_id).decide(
        str(approval.id),
        approver=Actor(id=approver_id, kind=ActorType.HUMAN, is_privileged_human=True),
        approve=True,
        note="Salary reviewed",
    )
    # A later edit to the task must not change the hash-verified approved draft.
    await tasks.set_output(str(task.id), {"jd": "Unapproved salary: 99 million VND"})
    await tasks.transition(str(task.id), Transition.APPROVAL_GRANTED)
    resumed = await service.execute_task(str(task.id))
    assert resumed.status == "completed"
    assert task.output["finding"] == "The rubric uses the salary approved by the human reviewer."
    assert (
        await seeded.session.execute(select(Approval.id).where(Approval.task_id == str(task.id)))
    ).scalars().all() == [approval.id]


@pytest.mark.parametrize("approve", [True, False])
async def test_one_approval_decision_resumes_or_stops_the_whole_local_goal(
    seeded, db, monkeypatch, approve
):
    from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
    from ai_orchestrator.api.approvals import ApprovalDecisionRequest, _decide
    from ai_orchestrator.application import local_runner
    from ai_orchestrator.application.task_execution import _resolve_auto_approver
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.models.gateway import ModelResponse
    from tests.integration.test_pipeline_runs_itself import _TieredRuntime
    from tests.integration.test_task_api_and_ui import _ctx
    from tests.unit.test_pydanticai_bridge import _ScriptedGateway

    class Runtime(_TieredRuntime):
        async def execute(self, task, context, **kwargs):
            if context.delegate_options:
                return await super().execute(task, context, **kwargs)
            response = (
                '{"finding": "The salary was reviewed; the rubric is ready for use."}'
                if context.task.input.get("approved_human_reviews")
                else '{"__approval_request__": "Review the salary before preparing a rubric", '
                '"output": {"jd": "The draft salary is 22 million VND"}}'
            )
            return await run_with_pydantic_ai(
                task, context, gateway=_ScriptedGateway(ModelResponse(text=response))
            )

    monkeypatch.setattr(
        "ai_orchestrator.application.pipeline.build_runtime", lambda *a, **k: Runtime()
    )
    monkeypatch.setattr(local_runner.Database, "from_settings", lambda *a, **k: db)
    root = await _root(
        seeded, goal="JD then rubric with a real review", contract={"required": ["finding"]}
    )
    first = await run_pipeline(db, seeded.organization_id, str(root.id), auto_approve=False)
    assert first.waiting_for_human and not first.finished
    async with db.tenant_session(seeded.organization_id) as session:
        row = (
            await session.execute(
                select(Approval).where(Approval.task_id.in_(first.waiting_for_human))
            )
        ).scalar_one()
        approver = await _resolve_auto_approver(session, seeded.organization_id)
        response = await _decide(
            str(row.id),
            ApprovalDecisionRequest(note="Reviewed salary"),
            _ctx(
                Actor(id=approver, kind=ActorType.HUMAN, is_privileged_human=True),
                organization_id=seeded.organization_id,
                session=session,
            ),
            approve=approve,
        )
        assert response["local_run"]["task_id"] == str(root.id)
    running = local_runner._running.get((seeded.organization_id, str(root.id)))
    if running:
        await asyncio.wait_for(running, 10)
    async with db.tenant_session(seeded.organization_id) as session:
        final = await TaskRepository(session, seeded.organization_id).get(str(root.id))
        assert final.status == ("completed" if approve else "failed"), final.last_error
        rows = (
            (
                await session.execute(
                    select(Approval).where(Approval.organization_id == seeded.organization_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == ("approved" if approve else "rejected")
        assert (
            not (await session.execute(select(Approval.id).where(Approval.status == "pending")))
            .scalars()
            .all()
        )


async def test_two_required_reviews_preserve_each_draft_and_wait_at_each_stage(seeded):
    from ai_orchestrator.application.task_execution import (
        TaskExecutionService,
        _resolve_auto_approver,
    )
    from ai_orchestrator.approvals.service import ApprovalService
    from ai_orchestrator.domain.contracts import Actor

    jd = {"salary_vnd": 22_000_000, "duties": "Inspect production quality and supplier audits"}
    rubric = {"quality": 70, "supplier_audits": 30}

    class StagedRuntime:
        name = "staged-human-reviews"
        calls = 0

        async def execute(self, task, context, **kwargs):
            self.calls += 1
            receipts = context.task.input["approved_human_reviews"]
            if receipts:
                assert receipts[0]["output"] == {"jd": jd}
            if len(receipts) == 2:
                assert receipts[1]["output"] == {"jd": jd, "rubric": rubric}
            # The producer always claims completion. The platform must enforce both reviews.
            return AgentResult(
                task_id=str(task.task_id),
                execution_id=str(task.execution_id),
                status=AgentResultStatus.COMPLETED,
                summary="Candidate Lan meets the rubric.",
                output={
                    "jd": jd,
                    "rubric": rubric,
                    "shortlist": ["Lan meets the approved requirements"],
                },
            )

    agent = await _agent(seeded.session, seeded.organization_id, "HR Agent")
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title="JD then rubric then shortlist",
        goal="Obtain separate human reviews for JD and rubric",
        task_type="analysis",
        requester_type="human",
        owner_agent_id=str(agent.id),
        expected_output_schema={
            "required": ["jd", "rubric", "shortlist"],
            "properties": {
                "jd": {"x-human-review-order": 1},
                "rubric": {"x-human-review-order": 2},
            },
        },
    )
    runtime = StagedRuntime()
    service = TaskExecutionService(
        seeded.session,
        seeded.organization_id,
        runtime=runtime,
        auto_approve=False,
    )
    approvals = ApprovalService(seeded.session, seeded.organization_id)
    signer = await _resolve_auto_approver(seeded.session, seeded.organization_id)
    ids = []
    for stage in range(2):
        paused = await service.execute_task(str(task.id))
        assert paused.status == "waiting_for_approval"
        assert "shortlist" not in task.output
        assert runtime.calls == stage + 1
        rows = (
            (
                await seeded.session.execute(
                    select(Approval).where(
                        Approval.task_id == str(task.id),
                        Approval.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        approval = rows[0]
        ids.append(str(approval.id))
        waiting = await service.execute_task(str(task.id))
        assert waiting.status == "waiting_for_approval"
        assert runtime.calls == stage + 1
        await approvals.decide(
            str(approval.id),
            approver=Actor(id=signer, kind=ActorType.HUMAN, is_privileged_human=True),
            approve=True,
            note="Reviewed this stage",
        )
    result = await service.execute_task(str(task.id))
    assert result.status == "completed"
    assert runtime.calls == 3
    assert len(set(ids)) == 2
    assert task.output["jd"] == jd and task.output["rubric"] == rubric
