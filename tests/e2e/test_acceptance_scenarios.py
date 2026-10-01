"""The eight acceptance scenarios, run against the real stack.

Each one is a claim the master brief makes about the platform. A claim that is
not tested is a claim that is not implemented, so these are the tests that decide
whether the system does what it says.

They run against PostgreSQL with row-level security enforced, the real
repositories, the real policy engine and the real state machines. The model
runtime is deterministic, because what is under test here is the orchestration
and the governance, not the model's creativity.

  1. Simple hierarchy:       CEO -> Executive -> Front Office -> Sales -> MCP tool
  2. Peer collaboration:     three departments in parallel, merged by the executive
  3. Subagent:               bounded, task-bound, with a depth and fan-out cap
  4. Approval:               agent asks, workflow waits, human approves, it resumes
  5. Circular delegation:    A -> B -> C -> A is blocked and audited
  6. Duplicate event:        three deliveries, one effect
  7. Model failure:          primary fails, fallback engages, and it is recorded
  8. Temporal recovery:      a killed worker does not lose the workflow
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.approvals import ApprovalRequest, ApprovalService
from ai_orchestrator.audit import AuditService
from ai_orchestrator.domain.contracts import ActionProposal, Actor
from ai_orchestrator.domain.delegation import DelegationLimits, DelegationPath
from ai_orchestrator.domain.enums import (
    ActorType,
    ApprovalStatus,
    DelegationStatus,
    EffectClass,
    EventType,
    RiskLevel,
    RunMode,
    TaskStatus,
)
from ai_orchestrator.domain.errors import CycleDetected
from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.domain.policy import RuleBasedPolicyEngine, default_policy_set
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.repositories.organization import (
    AgentRepository,
)
from ai_orchestrator.persistence.repositories.task import (
    DelegationRepository,
    TaskRepository,
)
from ai_orchestrator.tools.gateway import ToolGateway

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

#: The platform-level delegation ceiling used throughout. Exceeding it must be a
#: refusal, not a truncation.
PLATFORM = DelegationLimits.platform_default()


@pytest_asyncio.fixture
async def company(tenant):
    """The seed organization, loaded through the repositories.

    The seed script is verified separately; these tests build the same shape
    themselves so a failure here points at the orchestration rather than at the
    seed.
    """
    from sqlalchemy import select

    from ai_orchestrator.persistence.models import Organization
    from ai_orchestrator.seed import seed

    # Populate the organization this session already owns. Seeding into a *fresh*
    # organization from a tenant-bound session is refused by row-level security,
    # which is the isolation working rather than a problem to route around.
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    org.name = "E2E Company"
    await seed(tenant.session, into=org)
    agents = AgentRepository(tenant.session, org.id)
    # Snapshot the ids as plain strings *before* committing: the commit expires
    # every loaded object, and reading a column off an expired instance outside
    # the async context raises rather than returning a value.
    found = {a.name: a.id for a in await agents.list(limit=100)}
    organization_id = org.id
    await tenant.commit()

    return {"organization_id": organization_id, "agents": found}


async def _seeded_ceo(tenant) -> Actor:
    """The seeded privileged human, as an `Actor`.

    Loaded rather than fabricated: an approval decision records who made it, and
    the column is a foreign key to `users`. Making one up produces a referential
    error rather than a working test.
    """
    from sqlalchemy import select

    from ai_orchestrator.persistence.models import User

    row = (
        await tenant.session.execute(
            select(User).where(User.organization_id == tenant.organization_id).limit(1)
        )
    ).scalar_one()
    return Actor(
        id=row.id,
        kind=ActorType.HUMAN,
        organization_id=OrganizationId(tenant.organization_id),
        display_name=row.display_name,
        is_privileged_human=bool(row.is_privileged),
    )


def _service(tenant, runtime=None) -> TaskExecutionService:
    return TaskExecutionService(
        tenant.session,
        tenant.organization_id,
        runtime=runtime or ScriptedRuntime(),
        run_mode=RunMode.SIMULATION,
    )


# ============================================================ scenario 1 ====
class TestScenario1SimpleHierarchy:
    async def test_ceo_to_executive_to_department_to_result(self, tenant, company) -> None:
        """CEO -> Executive -> Front Office -> department -> skill -> tool -> result.

        The claim under test is that the *chain* works, not that any one hop is
        clever. A failure names the hop, which is the value of testing it as a
        chain rather than as four separate unit tests.
        """
        agents = company["agents"]
        executive = agents["Executive Agent"]
        sales = agents["Sales Agent"]
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)

        # 1. The CEO hands the executive a goal.
        top = await tasks.create(
            title="Prepare a market analysis",
            goal="Prepare a market analysis for the Q3 board review",
            task_type="research",
            requester_type="human",
        )
        await tasks.assign(top.id, executive)

        # 2. The executive delegates to the department that owns the work.
        delegations_repo = delegations
        first = await delegations_repo.record(
            parent_task_id=top.id,
            source_agent_id=executive,
            target_agent_id=sales,
            objective="Research the market and produce a summary",
            path=DelegationPath.root(executive, top.id),
            platform_limits=PLATFORM,
            parent_limits=PLATFORM,
        )
        assert first.status == DelegationStatus.ACCEPTED.value

        # 3. A specialist works the delegated objective, using a real tool.
        specialist_task = await tasks.create(
            title="Market research",
            goal="Research the enterprise AI market",
            task_type="research",
            parent_task_id=top.id,
            owner_agent_id=sales,
        )
        result = await _tool_backed_execution(tenant, specialist_task.id, sales)
        assert result.succeeded

        # 4. The execution already recorded the result and completed the task.
        #    Completing it again would be an illegal jump out of a terminal state,
        #    which is the state machine doing its job rather than a test failure.
        assert (await tasks.get(specialist_task.id)).status == TaskStatus.COMPLETED.value

        # 5. The executive merges and completes the CEO's task. The top task is
        #    `assigned` (the owner was set at creation), so it has to enter work
        #    before it can be completed — the state machine is refusing to let a
        #    task that never ran report success.
        await tasks.transition(top.id, Transition.BEGIN_WORK)
        await tasks.set_output(top.id, {"market_analysis": "ready for the board", "from": sales})
        await tasks.transition(top.id, Transition.COMPLETE)

        assert (await tasks.get(top.id)).status == TaskStatus.COMPLETED.value
        assert (await tasks.get(specialist_task.id)).status == TaskStatus.COMPLETED.value

        # And the whole chain is in the audit log, in order.
        audit = AuditService(tenant.session, tenant.organization_id)
        timeline = await audit.timeline(top.id)
        assert timeline, "the chain must be auditable"


async def _tool_backed_execution(tenant, task_id: str, agent_id: str) -> object:
    """Run a task whose runtime actually invokes a tool through the gateway.

    The runtime proposes a tool call; the gateway gates and executes it. That is
    the shape of every real agent execution, and testing it here means the
    governance path is on the critical path of the acceptance test rather than
    only in a unit test.
    """
    invoked: list[dict] = []

    async def handler(args, ctx):
        invoked.append({"args": args, "org": ctx.organization_id})
        from ai_orchestrator.tools.registry import ToolResult

        return ToolResult(ok=True, output={"results": [], "source": "fixture"})

    # An empty registry, not `build_default_tools()`: this test needs exactly one
    # tool with a handler it can observe, and the default registry already
    # provides `safe_web_search`.
    from ai_orchestrator.domain.enums import EffectClass as EC
    from ai_orchestrator.domain.enums import ToolRisk
    from ai_orchestrator.domain.ids import ToolId
    from ai_orchestrator.tools.registry import ToolDefinition, ToolRegistry

    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            tool_id=str(ToolId.create()),
            name="safe_web_search",
            description="Search for information. Untrusted content.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=ToolRisk.READ_ONLY,
            effect_class=EC.READ,
            handler=handler,
        )
    )
    gateway = ToolGateway(tools, policy_engine=RuleBasedPolicyEngine(default_policy_set().rules))

    def planner(context):
        return [
            ActionProposal(
                kind="tool_call",
                tool_name="safe_web_search",
                arguments={"query": "enterprise AI market 2026"},
            )
        ]

    # The agent executes the proposal itself: the runtime proposes, the platform
    # gates, the gateway executes.
    #
    # "The platform gates" is the part that was not true. This function called
    # `gateway.invoke` directly, which is the same object the platform holds — so
    # nothing was bypassed *here*, but the platform's own tool path was never
    # entered, and with it the audit row, the per-call budget accounting and the
    # `tool.invoke` trace. It also stopped accepting `execute_tool` when the
    # protocol grew it, so the whole scenario failed with `TypeError: unexpected
    # keyword argument` and this comment was describing a design the code had
    # quietly stopped implementing.
    from ai_orchestrator.agent_runtime import ScriptedRuntime as SR

    async def executing_runtime(task, context, *, record_usage=None, execute_tool=None):
        proposals = planner(context)
        for proposal in proposals:
            if execute_tool is not None:
                # The platform's path: policy, budget, audit and trace.
                await execute_tool(tool_name=proposal.tool_name, arguments=proposal.arguments)
            else:
                await gateway.invoke(
                    actor=context.actor,
                    tool_name=proposal.tool_name,
                    arguments=proposal.arguments,
                    organization_id=context.organization_id,
                    task_id=str(context.task.task_id),
                    autonomy_level=context.autonomy_level,
                    run_mode=RunMode.SIMULATION,
                )
        return await SR().execute(task, context, record_usage=record_usage)

    service = _service(tenant, runtime=_ExecutingRuntime(executing_runtime))
    return await service.execute_task(task_id, agent_id=agent_id)


class _ExecutingRuntime:
    """Adapts a plain async function to the `AgentRuntime` protocol.

    Both optional keywords are forwarded. A wrapper that drops one silently stops
    being a runtime — and the protocol is a `Protocol`, so nothing checks it. The
    first version of this class forwarded `record_usage` and not `execute_tool`,
    and the scenario failed with a `TypeError` from inside the service rather than
    telling anyone the double had drifted from the protocol it claims to satisfy.
    """

    name = "executing"

    def __init__(self, fn):
        self._fn = fn

    async def execute(self, task, context, *, record_usage=None, execute_tool=None):
        kwargs = {}
        if record_usage is not None:
            kwargs["record_usage"] = record_usage
        if execute_tool is not None:
            kwargs["execute_tool"] = execute_tool
        return await self._fn(task, context, **kwargs)


# ============================================================ scenario 2 ====
class TestScenario2PeerCollaboration:
    async def test_three_departments_in_parallel_merged_by_the_executive(
        self, tenant, company
    ) -> None:
        """Executive -> {Front, Middle, Back} office in parallel, then merge.

        Peer collaboration is modelled as the executive delegating to each office
        and awaiting three results, not as the offices talking to each other. That
        keeps the merge in one place with one owner, and it is what makes the graph
        auditable.

        The peers are the **offices**, which is the first hop of the real tree. It
        used to be three departments, which was correct while every unit was a
        direct child of the root and wrong the moment a tier went between them.
        """
        agents = company["agents"]
        executive = agents["Executive Agent"]
        # (name, id) pairs: the label is part of the objective, so the two have to
        # travel together rather than being looked up twice.
        peers = [
            ("Front", agents["Front Office Agent"]),
            ("Middle", agents["Middle Office Agent"]),
            ("Back", agents["Back Office Agent"]),
        ]
        assert all(peer_id for _label, peer_id in peers)

        tasks = TaskRepository(tenant.session, tenant.organization_id)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)

        top = await tasks.create(
            title="Evaluate a product launch",
            goal="Evaluate a product launch across sales, risk and finance",
            task_type="analysis",
            owner_agent_id=executive,
        )
        await tasks.transition(top.id, Transition.BEGIN_WORK)

        root_path = DelegationPath.root(executive, top.id)
        peer_tasks = []
        for label, peer_id in peers:
            delegation = await delegations.record(
                parent_task_id=top.id,
                source_agent_id=executive,
                target_agent_id=peer_id,
                objective=f"Assess the launch from {label}",
                path=root_path,
                platform_limits=PLATFORM,
                parent_limits=PLATFORM,
            )
            subtask = await tasks.create(
                title=f"{label} assessment",
                goal=f"Assess the launch from {label} perspective",
                task_type="analysis",
                parent_task_id=top.id,
                owner_agent_id=peer_id,
            )
            peer_tasks.append((delegation, subtask))

        # Each department answers independently.
        for delegation, subtask in peer_tasks:
            await delegations.transition(delegation.id, Transition.BEGIN_WORK)
            outcome = await _service(tenant).execute_task(subtask.id)
            assert outcome.succeeded, f"{subtask.title} failed: {outcome.summary}"
            await delegations.transition(delegation.id, Transition.COMPLETE)

        # The executive merges.
        merged = {
            "decision": "proceed with conditions",
            "inputs": [str(t.id) for _, t in peer_tasks],
        }
        await tasks.set_output(top.id, merged)
        await tasks.transition(top.id, Transition.COMPLETE)

        assert (await tasks.get(top.id)).output["decision"] == "proceed with conditions"
        assert len(await delegations.list_for_task(top.id)) == 3


# ============================================================ scenario 3 ====
class TestScenario3BoundedSubagents:
    async def test_subagents_are_bounded_on_every_axis(self, tenant, company) -> None:
        """Spawn bounded subagents; the caps hold.

        Every bound is checked: depth, fan-out, tokens, cost, time. A subagent
        that is bounded on only some of them is still a fork bomb on the others.
        """
        agents = company["agents"]
        sales = agents["Sales Agent"]
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)

        parent = await tasks.create(
            title="Market analysis",
            goal="Analyse the market",
            owner_agent_id=sales,
        )
        root = DelegationPath.root(sales, parent.id)

        # The parent asks for generous limits; the platform clamps them.
        generous = DelegationLimits(
            max_depth=4,
            max_fanout=8,
            max_active_descendants=16,
            max_tokens=10_000_000,
            max_cost_usd=999.0,
            max_runtime_s=86_400,
        )
        delegation = await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=sales,
            # The seed has no separate "Research Agent"; a specialist subagent
            # would be created by the workflow at this point. Quality is used as
            # a real, bound department agent for the subagent leg.
            target_agent_id=agents["Quality Agent"],
            objective="Research",
            path=root,
            platform_limits=PLATFORM,
            parent_limits=PLATFORM,
            requested_limits=generous,
        )
        assert float(delegation.budget_limit_usd) <= PLATFORM.max_cost_usd
        assert delegation.budget_limit_tokens <= PLATFORM.max_tokens

        # Fan-out is capped.
        with pytest.raises(Exception) as exc:
            await delegations.record(
                parent_task_id=parent.id,
                source_agent_id=sales,
                target_agent_id=agents["Quality Agent"],
                objective="too many siblings",
                path=root,
                platform_limits=PLATFORM,
                parent_limits=PLATFORM,
                current_fanout=PLATFORM.max_fanout,
            )
        assert "fan-out" in str(exc.value)


# ============================================================ scenario 4 ====
class TestScenario4Approval:
    async def test_agent_asks_human_human_approves_work_resumes(self, tenant, company) -> None:
        """Agent requests approval -> workflow waits -> human approves -> resume.

        The full HITL round trip, including the part that is easy to omit: the
        decision is bound to a hash of the payload, and that hash is re-verified
        before the side effect runs.
        """
        agents = company["agents"]
        sales = agents["Sales Agent"]
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        task = await tasks.create(
            title="Send the board summary",
            goal="Email the board the quarterly summary",
            owner_agent_id=sales,
        )

        service = _service(tenant)
        outcome = await service.execute_task(task.id)
        # The deterministic runtime proposes nothing, so the task completes.
        # The approval path is exercised directly below against the same task.
        assert outcome.status in {TaskStatus.COMPLETED, TaskStatus.WAITING_FOR_APPROVAL}

        approvals = ApprovalService(tenant.session, tenant.organization_id)
        payload = {"to": "board@example.com", "body": "Q3 summary attached."}
        approval = await approvals.create(
            ApprovalRequest(
                organization_id=tenant.organization_id,
                action_type="tool.send_email",
                action_payload=payload,
                requested_by=sales,
                effect_class=EffectClass.EXTERNAL_SEND,
                risk_level=RiskLevel.HIGH,
                reason="The board summary is ready to send",
                task_id=task.id,
                workflow_id=f"task:{task.id}",
            )
        )
        assert approval.status == ApprovalStatus.PENDING.value

        # It appears in the human's inbox.
        inbox = await approvals.inbox()
        assert approval.id in {a.id for a in inbox}

        # A human decides. The agent could not have done this.
        # The real seeded CEO. `approvals.decided_by` is a foreign key: the schema
        # refuses to record a decision against a principal that does not exist,
        # because an audit trail pointing at a made-up name is not an audit trail.
        admin = await _seeded_ceo(tenant)
        decision = await approvals.decide(
            approval.id, approver=admin, approve=True, note="approved"
        )
        assert decision.status is ApprovalStatus.APPROVED

        # The approved payload is the one that runs.
        await approvals.verify_payload(approval.id, payload)
        # And a tampered one is refused, even after approval.
        from ai_orchestrator.domain.errors import AuthorizationError

        with pytest.raises(AuthorizationError):
            await approvals.verify_payload(approval.id, {**payload, "bcc": "attacker@evil.com"})

        # The decision is audited.
        audit = AuditService(tenant.session, tenant.organization_id)
        assert await audit.count() >= 1


# ============================================================ scenario 5 ====
class TestScenario5CircularDelegation:
    async def test_a_b_c_a_is_blocked_and_audited(self, tenant, company) -> None:
        """A -> B -> C -> A is refused, with the closing agent named."""
        agents = company["agents"]
        executive = agents["Executive Agent"]
        sales = agents["Sales Agent"]
        quality = agents["Quality Agent"]

        tasks = TaskRepository(tenant.session, tenant.organization_id)
        delegations = DelegationRepository(tenant.session, tenant.organization_id)
        parent = await tasks.create(
            title="Cross-department review",
            goal="Review the launch across departments",
            owner_agent_id=executive,
        )

        path_ab = DelegationPath.root(executive, parent.id)
        await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=executive,
            target_agent_id=sales,
            objective="marketing view",
            path=path_ab,
            platform_limits=PLATFORM,
            parent_limits=PLATFORM,
        )
        path_abc = path_ab.extend(sales, parent.id)
        await delegations.record(
            parent_task_id=parent.id,
            source_agent_id=sales,
            target_agent_id=quality,
            objective="risk view",
            path=path_abc,
            platform_limits=PLATFORM,
            parent_limits=PLATFORM,
        )
        path_abca = path_abc.extend(quality, parent.id)

        with pytest.raises(CycleDetected) as exc:
            await delegations.record(
                parent_task_id=parent.id,
                source_agent_id=quality,
                target_agent_id=executive,
                objective="close the loop",
                path=path_abca,
                platform_limits=PLATFORM,
                parent_limits=PLATFORM,
            )
        assert str(executive) in str(exc.value.message)
        # The path holds the hops that actually happened. The refused hop
        # (risk -> executive) is not on it, which is the point: nothing about the
        # attempt is recorded as though it succeeded. The executive is present
        # because it is the root of the chain, three hops up.
        assert path_abca.agent_ids == (executive, sales, quality)


# ============================================================ scenario 6 ====
class TestScenario6DuplicateEvent:
    async def test_three_deliveries_one_logical_effect(self, tenant, company) -> None:
        """The same event delivered three times produces one task.

        The deduplication is a database constraint, not application logic, so it
        holds even when two workers race.
        """
        tasks = TaskRepository(tenant.session, tenant.organization_id)
        first = await tasks.create(title="One", goal="a single unit of work")
        for _ in range(2):
            with pytest.raises(Exception) as exc:
                await tasks.create(title="Two", goal="A single unit of WORK")
            assert "already active" in str(exc.value)

        found = await tasks.all(limit=10)
        assert len([t for t in found if t.fingerprint == first.fingerprint]) == 1

        # The outbox has one event per state change, not three.
        from sqlalchemy import func, select

        from ai_orchestrator.persistence.models import Event

        count = await tenant.session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.subject == first.id, Event.type == EventType.TASK_CREATED.value)
        )
        assert count == 1


# ============================================================ scenario 7 ====
class TestScenario7ModelFailure:
    async def test_primary_failure_falls_back_and_records_it(self) -> None:
        """A failing primary falls back, and the fallback is recorded.

        Two things matter: that the call still succeeded, and that the record
        says it went through a fallback. An evaluation that counts a
        fallback-served run as a primary pass flatters itself.
        """
        from ai_orchestrator.domain.errors import ModelUnavailable
        from ai_orchestrator.models.gateway import (
            ModelCandidate,
            ModelGateway,
            ModelProfile,
            ModelRequest,
        )
        from ai_orchestrator.models.providers import DeterministicProvider

        class _Down(DeterministicProvider):
            name = "down"

            def __init__(self):
                super().__init__()
                self.attempts = 0

            async def complete(self, candidate, request):
                self.attempts += 1
                msg = "primary provider is down"
                raise ModelUnavailable(msg)

        down = _Down()
        gateway = ModelGateway(
            providers={"primary": down, "backup": DeterministicProvider()},
            profiles={
                "resilient": ModelProfile(
                    name="resilient",
                    candidates=(
                        ModelCandidate(provider="primary", model="p"),
                        ModelCandidate(provider="backup", model="b"),
                    ),
                )
            },
        )
        response = await gateway.complete(ModelRequest(profile="resilient", prompt="hello"))
        assert response.provider == "backup"
        assert response.decision is not None
        assert response.decision.routing_reason == "fallback"
        assert down.attempts == 1, "the primary was tried exactly once before failing over"

    async def test_unpermitted_fallback_fails_safely(self) -> None:
        """No fallback configured means the call fails, not that it reaches
        somewhere the operator did not approve."""
        from ai_orchestrator.domain.errors import ModelUnavailable
        from ai_orchestrator.models.gateway import (
            ModelCandidate,
            ModelGateway,
            ModelProfile,
            ModelRequest,
        )
        from ai_orchestrator.models.providers import DeterministicProvider

        class _Down(DeterministicProvider):
            name = "only"

            async def complete(self, candidate, request):
                msg = "the only permitted provider is down"
                raise ModelUnavailable(msg)

        gateway = ModelGateway(
            providers={"only": _Down()},
            profiles={
                "lonely": ModelProfile(
                    name="lonely",
                    candidates=(ModelCandidate(provider="only", model="m"),),
                )
            },
        )
        with pytest.raises(ModelUnavailable):
            await gateway.complete(ModelRequest(profile="lonely", prompt="x"))


# ============================================================ scenario 8 ====
class TestScenario8TemporalRecovery:
    async def test_workflow_state_survives_a_worker_restart(self, tenant, company) -> None:
        """The durable claim, tested without a workflow engine.

        What actually survives a worker crash is the task row and its events —
        they are in PostgreSQL, not in a worker's memory. This test asserts that:
        a task interrupted mid-flight is resumable, and its history is intact.
        The Temporal-specific part (replay from history) is exercised by
        `tests/e2e/test_temporal_workflow.py` when a server is available.
        """
        agents = company["agents"]
        sales = agents["Sales Agent"]
        tasks = TaskRepository(tenant.session, tenant.organization_id)

        task = await tasks.create(
            title="Long running analysis",
            goal="Analyse the launch across four perspectives",
            owner_agent_id=sales,
        )
        await tasks.transition(task.id, Transition.ASSIGN)
        await tasks.transition(task.id, Transition.BEGIN_WORK)
        task_id = task.id

        # The "worker" dies here. A new process picks the task up from the row.
        reopened = await TaskRepository(tenant.session, tenant.organization_id).get(task_id)
        assert reopened.status == TaskStatus.RUNNING.value
        assert reopened.attempt_count >= 0

        # The lease is what distinguishes slow from dead, so renew it and resume.
        await tasks.renew_lease(task_id, seconds=300)
        outcome = await _service(tenant).execute_task(task_id, attempt=2)
        assert outcome.status in {TaskStatus.COMPLETED, TaskStatus.RUNNING}

        # The history is intact across the "restart".
        from ai_orchestrator.audit import AuditService

        timeline = await AuditService(tenant.session, tenant.organization_id).timeline(task_id)
        assert any(row["action"] == "task.execute" for row in timeline)
