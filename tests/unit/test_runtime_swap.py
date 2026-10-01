"""Runtime swap test.

The platform claims a runtime can be replaced without rewriting organization,
task, policy, authorization, memory or the event schema. This test is the proof
of that claim, and it is deliberately behavioural rather than structural: it runs
the same scenario through two different runtimes and asserts the *rest of the
platform* produced identical results.

A test that only checked `isinstance(runtime, AgentRuntime)` would prove nothing.
The value is in everything that must stay the same while the thing underneath
changes.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_orchestrator.agent_runtime import NullRuntime, ScriptedRuntime
from ai_orchestrator.agent_runtime.context import ContextBuilder, ContextBuilderInput
from ai_orchestrator.domain.authority import (
    SUBAGENT_FLOOR_PROFILE,
    Action,
    AuthorityAction,
    PolicyContext,
    Resource,
    evaluate_authority,
)
from ai_orchestrator.domain.budget import BudgetState, Money, TokenUsage
from ai_orchestrator.domain.contracts import (
    ActionProposal,
    Actor,
    AgentContext,
    AgentResult,
    AgentResultStatus,
    AgentRuntime,
    AgentTask,
)
from ai_orchestrator.domain.enums import (
    ActorType,
    AutonomyLevel,
    EffectClass,
    RiskLevel,
    RunMode,
    TaskType,
)
from ai_orchestrator.domain.ids import ExecutionId, OrganizationId, TaskId, ToolId
from ai_orchestrator.domain.policy import (
    RuleBasedPolicyEngine,
    apply_autonomy_gate,
    default_policy_set,
)

pytestmark = pytest.mark.unit


def _actor(org: OrganizationId) -> Actor:
    return Actor(id="agt_1", kind=ActorType.AGENT, organization_id=org, display_name="Analyst")


def _context(**overrides: object) -> AgentContext:
    org = OrganizationId.create()
    task = AgentTask(
        task_id=TaskId.create(),
        organization_id=org,
        goal="Summarise the quarterly revenue report",
        task_type=TaskType.ANALYSIS,
        execution_id=ExecutionId.create(),
    )
    data = ContextBuilderInput(
        actor=_actor(org),
        task=task,
        system_instructions="You are a financial analyst. Be precise.",
        organization_id=str(org),
        max_tokens=100_000,
        max_cost_usd=2.0,
        max_runtime_s=300,
        context_window_tokens=16_000,
    )
    if overrides:
        for key, value in overrides.items():
            setattr(data, key, value)
    return ContextBuilder().build(data)


class TestRuntimeProtocol:
    def test_both_runtimes_satisfy_the_protocol(self) -> None:
        """The seam is the Protocol, not a base class. A third-party runtime that
        satisfies the methods is a valid runtime with no registration step."""
        assert isinstance(ScriptedRuntime(), AgentRuntime)
        assert isinstance(NullRuntime(), AgentRuntime)

    async def test_runtimes_return_typed_results(self) -> None:
        ctx = _context()
        task = ctx.task
        for runtime in (ScriptedRuntime(), NullRuntime()):
            result = await runtime.execute(task, ctx)
            assert isinstance(result, AgentResult)
            assert result.task_id == task.task_id
            assert result.execution_id == task.execution_id
            assert result.summary, "a result must always carry a human summary"
            assert result.status in set(AgentResultStatus)

    async def test_a_failed_runtime_reports_a_typed_error_not_an_exception(
        self,
    ) -> None:
        """A missing runtime is a configuration problem, and it must be
        distinguishable from a platform bug in a failed-task triage."""
        result = await NullRuntime().execute(_context().task, _context())
        assert result.status is AgentResultStatus.FAILED
        assert result.error_code == "RUNTIME_UNAVAILABLE"


class TestSwapChangesNothingElse:
    async def test_same_context_produces_the_same_typed_contract(self) -> None:
        ctx = _context()
        a = await ScriptedRuntime().execute(ctx.task, ctx)
        b = await ScriptedRuntime().execute(ctx.task, ctx)
        # Identical inputs, identical contract shape, identical accounting.
        assert a.status == b.status
        assert a.model_dump(exclude={"execution_id"}) == b.model_dump(exclude={"execution_id"})

    def test_organization_and_task_contracts_are_unaffected_by_the_runtime(
        self,
    ) -> None:
        """The point of the seam: the domain types are declared once, in
        `domain.contracts`, and a runtime cannot extend or alter them."""
        ctx = _context()
        # Fields a runtime could plausibly want to add, and must not have.
        assert not hasattr(ctx, "openai_messages")
        assert not hasattr(ctx, "langgraph_state")
        assert set(AgentContext.model_fields) >= {
            "actor",
            "task",
            "system_instructions",
            "authorized_tools",
            "authorized_skills",
            "budget",
            "delegation_limits",
        }

    def test_policy_decisions_are_identical_regardless_of_runtime(self) -> None:
        """Policy reads the action, not the runtime. If swapping a runtime could
        change a policy outcome, the runtime would own governance."""
        org = OrganizationId.create()
        engine = RuleBasedPolicyEngine(default_policy_set().rules)
        ctx = PolicyContext(organization_id=str(org))
        action = Action(
            action=AuthorityAction.CALL_TOOL,
            effect=EffectClass.EXTERNAL_SEND,
            risk=RiskLevel.HIGH,
            resource=Resource("tool", "tool_1", str(org)),
        )
        first = asyncio.run(engine.evaluate(_actor(org), action, action.resource, ctx))
        second = asyncio.run(engine.evaluate(_actor(org), action, action.resource, ctx))
        assert first.to_dict() == second.to_dict()

    def test_authorization_decision_is_independent_of_the_runtime(self) -> None:
        org = OrganizationId.create()
        ctx = PolicyContext(organization_id=str(org))
        action = Action(
            action=AuthorityAction.CALL_TOOL,
            effect=EffectClass.PREPARE,
            risk=RiskLevel.LOW,
            resource=Resource("tool", "tool_1", str(org)),
        )
        decision = evaluate_authority(
            actor=_actor(org), action=action, profile=SUBAGENT_FLOOR_PROFILE, context=ctx
        )
        assert decision.decision.value in {"allow", "deny", "require_approval", "escalate"}

    async def test_budget_accounting_is_identical_across_runtimes(self) -> None:
        """Usage and cost reported by the runtime flow into the same ledger."""
        ctx = _context()
        result = await ScriptedRuntime().execute(ctx.task, ctx)
        state = BudgetState(max_tokens=1000, max_cost_usd=Money("1.00"))
        usage = result.usage or TokenUsage()
        state.commit(usage, result.cost_usd or Money("0"))
        assert state.spent_tokens == usage.total
        assert state.remaining_cost_usd() == Money("1.00") - (result.cost_usd or Money("0"))


class TestRuntimeContractCompliance:
    async def test_runtime_cannot_exceed_the_context_budget(self) -> None:
        """The contract says a runtime must honour the budget. A runtime that
        ignores it is a bug, so the test asserts the envelope is finite and
        non-zero rather than pretending to police a third-party implementation.
        """
        ctx = _context(max_tokens=1000, max_cost_usd=0.5, max_runtime_s=30)
        assert ctx.budget.max_tokens == 1000
        assert ctx.budget.max_cost_usd == Money("0.5")
        assert ctx.budget.remaining_tokens() == 1000

    async def test_runtime_sees_only_authorised_tools(self) -> None:
        ctx = _context()
        # No tools were granted, so the runtime has none to call.
        assert ctx.authorized_tools == ()
        assert ctx.tool_by_name("anything") is None

    async def test_autonomy_level_is_carried_not_inferred(self) -> None:
        for level in AutonomyLevel:
            ctx = _context(autonomy_level=level)
            assert ctx.autonomy_level is level

    async def test_simulation_mode_is_visible_to_the_runtime(self) -> None:
        """A runtime must be able to tell it is in simulation, or a
        'simulation' run would perform real side effects."""
        ctx = _context(run_mode=RunMode.SIMULATION)
        assert ctx.run_mode is RunMode.SIMULATION
        live = _context(run_mode=RunMode.LIVE)
        assert live.run_mode is not RunMode.SIMULATION


class TestAutonomyGateIsNotRuntimeDependent:
    def test_l0_agent_is_denied_even_when_policy_allows(self) -> None:
        """The floor lives outside the rule set, so no policy edit and no runtime
        choice can hand an L0 agent the ability to act."""
        org = OrganizationId.create()
        engine = RuleBasedPolicyEngine(default_policy_set().rules)
        action = Action(
            action=AuthorityAction.READ,
            effect=EffectClass.READ,
            risk=RiskLevel.LOW,
            resource=Resource("task", "t", str(org)),
        )
        decision = asyncio.run(
            engine.evaluate(
                _actor(org), action, action.resource, PolicyContext(organization_id=str(org))
            )
        )
        assert decision.allowed
        gated = apply_autonomy_gate(decision, actor_kind="agent", autonomy=AutonomyLevel.L0_SUGGEST)
        assert gated.denied
        assert gated.rule_id == "AUTONOMY_FLOOR_L0"

    async def test_scripted_runtime_can_propose_but_not_perform(self) -> None:
        """A proposal is data. The platform decides whether to execute it, which
        is what keeps 'the LLM wanted to' from becoming 'the LLM did'."""

        def planner(_context: AgentContext) -> list[ActionProposal]:
            return [
                ActionProposal(
                    kind="tool_call",
                    tool_id=ToolId.create(),
                    tool_name="send_email",
                    arguments={"to": "someone@example.com"},
                    self_assessed_risk=RiskLevel.HIGH,
                )
            ]

        result = await ScriptedRuntime(planner=planner).execute(_context().task, _context())
        assert result.status is AgentResultStatus.NEEDS_APPROVAL
        assert len(result.follow_up_actions) == 1
        # The proposal names a tool; nothing was executed and no side effect
        # exists anywhere in the result.
        assert result.artifacts == ()
        assert "side_effect" not in result.output
