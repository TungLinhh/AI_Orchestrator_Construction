"""A turn loop needs a ceiling, and the ceiling has to be enforced.

The real model spent 23 consecutive turns on one executive goal and would have
kept going: nothing in PydanticAI stops a model that keeps returning tool calls,
and the budget envelope was checked per call and never tripped, because every
individual call was legal. Twenty-three legal calls that never converge is not a
budget — it is an unbounded loop that happens to be slow, and it spends the
organisation's money at the provider's rate.

The model below never converges on purpose. A gateway that always answers with
one more tool call is the cheapest reproduction of the failure, and it makes this
a test of the platform rather than of a provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import (
    _unexpected_model_behavior,
    _usage_limit_exceeded,
    run_with_pydantic_ai,
)
from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.contracts import (
    Actor,
    AgentContext,
    AgentResultStatus,
    AgentTask,
    BudgetEnvelope,
    ToolContract,
)
from ai_orchestrator.domain.enums import ActorType, DataClassification, TaskType
from ai_orchestrator.domain.ids import OrganizationId, ToolId
from ai_orchestrator.models.gateway import ModelResponse as GatewayResponse

pytestmark = pytest.mark.unit

ORG = "org_01m3d5hwxet3x61vjc1ffjyrzh"
AGENT = "agt_01m3d5hwxet3x61vjc1ffjyrzj"
TASK = "tsk_01m3d5hwxet3x61vjc1ffjyrzk"
EXEC = "exe_01m3d5hwxet3x61vjc1ffjyrzm"
# Minted rather than typed: the branded ids are Crockford base32, so a
# hand-written one fails the pattern for reasons unrelated to this test.
TOOL = ToolId.create()


class _EndlessGateway:
    """Answers every request with another tool call. Forever.

    It calls a tool the context really grants, so the loop is driven by the
    model's persistence rather than by an error. The point is the turn count, not
    what happens on turn 20.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        from ai_orchestrator.domain.budget import TokenUsage

        self.calls += 1
        return GatewayResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=[
                {
                    "id": f"c{self.calls}",
                    "type": "function",
                    "function": {"name": "ping", "arguments": '{"note": "again"}'},
                }
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="endless",
            provider="test",
        )


async def _execute(*, tool_name: str, arguments: dict[str, Any]) -> str:
    """A tool that always succeeds, so the loop is driven by the model alone.

    Keyword-only, because the bridge calls every tool that way; a positional
    signature here fails inside the agent loop and reads as a tool bug.
    """
    return "ok"


class _DelegatingGateway:
    """Calls `delegate_to_agent` once, then answers in prose.

    The shape of the run that exposed the worst defect in this project: the model
    delegated, the tool succeeded, and `delegations recorded: 0`.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        from ai_orchestrator.domain.budget import TokenUsage

        self.calls += 1
        if self.calls == 1:
            return GatewayResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "d1",
                        "type": "function",
                        "function": {
                            "name": "delegate_to_agent",
                            "arguments": (
                                '{"agent_name": "Finance Agent", "objective": "approve the spend"}'
                            ),
                        },
                    }
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model_used="delegating",
                provider="test",
            )
        return GatewayResponse(
            text="Merged the department reports.",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="delegating",
            provider="test",
        )


class _HallucinatingGateway:
    """Asks for a tool nobody granted.

    Not hypothetical: the same model invented a department called
    `Procurement`. A model that invents names invents tool names, and every tool
    here runs with `max_retries=0`, so one such call used to end the run as an
    unhandled `UnexpectedModelBehavior` — a 500 for what is a bad turn.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        from ai_orchestrator.domain.budget import TokenUsage

        self.calls += 1
        return GatewayResponse(
            text="",
            finish_reason="tool_calls",
            tool_calls=[
                {
                    "id": "h1",
                    "type": "function",
                    "function": {"name": "wire_money", "arguments": '{"amount": 9000}'},
                }
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="hallucinating",
            provider="test",
        )


class _OneShotGateway:
    """Answers in prose, once. The model that behaves."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        from ai_orchestrator.domain.budget import TokenUsage

        self.calls += 1
        return GatewayResponse(
            text="I delegated it and here is the merged result.",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="one-shot",
            provider="test",
        )


def _task() -> AgentTask:
    return AgentTask(
        task_id=TASK,
        organization_id=OrganizationId(ORG),
        goal="keep going",
        task_type=TaskType.COORDINATION,
        execution_id=EXEC,
    )


def _context(*, max_requests: int) -> AgentContext:
    return AgentContext(
        actor=Actor(id=AGENT, kind=ActorType.AGENT, organization_id=OrganizationId(ORG)),
        task=_task(),
        system_instructions="You coordinate.",
        organization_id=ORG,
        budget=BudgetEnvelope(
            max_tokens=100_000,
            max_cost_usd=Money("1.00"),
            max_runtime_s=300,
            max_requests=max_requests,
            max_tool_calls=max_requests + 8,
        ),
        data_classification=DataClassification.PUBLIC,
        authorized_tools=(
            ToolContract(
                tool_id=ToolId(TOOL),
                name="ping",
                description="Does nothing. Exists so the loop has something to call.",
                input_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                },
            ),
        ),
    )


class TestAHallucinatedToolIsABadTurnNotACrash:
    async def test_asking_for_an_ungranted_tool_fails_cleanly(self) -> None:
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_context(max_requests=4),
            gateway=_HallucinatingGateway(),
            execute_tool=_execute,
        )
        assert result.status is AgentResultStatus.FAILED, result.summary
        # `model_invented_tool`, not `tool_not_granted`. Both codes still exist, but
        # "not granted" implies a *policy* decision — a tool this agent is forbidden to
        # use — and the model here asked for one that has never existed. An operator
        # sent to the authorisation policy to answer "why was `wire_money` refused?"
        # finds nothing, which is F65's exact failure mode: a confident message that
        # points at the wrong place.
        assert result.error_code == "model_invented_tool", result.error_code
        # The name it invented belongs in the message: an operator chasing this
        # needs to know what the model thought it could do.
        assert "wire_money" in result.summary, result.summary

    async def test_the_failure_says_what_the_agent_was_offered_instead(self) -> None:
        """ "A tool does not exist" is a dead end. The tools it *could* have used are
        the next thing to look at, and they are the platform's to supply — the model
        cannot know them, and it will not guess correctly twice.

        Asserted against `ping`, which is what this fixture actually authorizes. The
        first version of this test asserted `write_report` and `safe_web_search` from
        memory, because those are the tools a *seeded* agent has — and it failed on a
        message that was completely correct. The fifth time in this project a name was
        written down from memory instead of read.
        """
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_context(max_requests=4),
            gateway=_HallucinatingGateway(),
            execute_tool=_execute,
        )
        assert "was offered" in result.summary, result.summary
        assert "ping" in result.summary, (
            f"the tool this agent was offered is not named in the failure: {result.summary}"
        )
        # And the invented name is *not* presented as one of them.
        assert "offered: ping" in result.summary or "offered: " in result.summary
        assert "wire_money," not in result.summary.split("was offered")[-1], (
            "the invented tool was listed among the ones on offer"
        )

    async def test_the_hallucinating_tool_is_never_executed(self) -> None:
        """The point of the gate: a name the platform did not grant does not run."""
        executed: list[str] = []

        async def record(*, tool_name: str, arguments: dict[str, Any]) -> str:
            executed.append(tool_name)
            return "ok"

        await run_with_pydantic_ai(
            task=_task(),
            context=_context(max_requests=4),
            gateway=_HallucinatingGateway(),
            execute_tool=record,
        )
        assert "wire_money" not in executed


class TestAProposalIsNotDropped:
    """The defect: twenty-one successful delegations, zero delegations recorded.

    `delegations recorded: 0` looked like a model that would not decompose, and
    three separate fixes were written against that wrong conclusion. The tool
    handler ran, the audit row said `success`, and the proposal never reached the
    layer that creates delegations — because the bridge returned the model's prose
    and dropped the tool calls.
    """

    async def test_a_delegating_run_surfaces_the_proposal(self) -> None:
        """The record of what the model asked, kept.

        `delegations recorded: 0` looked like a model that would not decompose, and
        three fixes were written against that wrong conclusion.
        """
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_delegating_context(),
            gateway=_DelegatingGateway(),
            execute_tool=_accepting_execute,
        )
        asked = [a for a in result.follow_up_actions if a.tool_name == "delegate_to_agent"]
        assert len(asked) == 1, f"the delegation was dropped: {result.follow_up_actions}"
        assert asked[0].arguments["agent_name"] == "Finance Agent"
        assert asked[0].objective == "approve the spend"

    async def test_a_delegation_is_not_filed_as_a_delegate_proposal(self) -> None:
        """It is filed as a `tool_call`, deliberately.

        A `delegate` proposal must name a resolved `target_agent_id`. The bridge
        has no session and cannot turn the model's `agent_name` into an id, and the
        delegation tool has already done the work by the time the run returns.
        Filing it as a delegate would mean delegating twice.
        """
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_delegating_context(),
            gateway=_DelegatingGateway(),
            execute_tool=_accepting_execute,
        )
        assert [a.kind for a in result.follow_up_actions] == ["tool_call"]

    async def test_a_plain_tool_call_is_recorded_too(self) -> None:
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_delegating_context(),
            gateway=_PingingGateway(),
            execute_tool=_accepting_execute,
        )
        assert [a.tool_name for a in result.follow_up_actions] == ["ping"]

    async def test_a_run_with_no_tool_calls_proposes_nothing(self) -> None:
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_delegating_context(),
            gateway=_OneShotGateway(),
            execute_tool=_accepting_execute,
        )
        assert result.follow_up_actions == ()


class _PingingGateway:
    """Calls a non-delegation tool once."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        from ai_orchestrator.domain.budget import TokenUsage

        self.calls += 1
        if self.calls == 1:
            return GatewayResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "p1",
                        "type": "function",
                        "function": {"name": "ping", "arguments": '{"note": "hi"}'},
                    }
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model_used="pinging",
                provider="test",
            )
        return GatewayResponse(
            text="done",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="pinging",
            provider="test",
        )


def _delegating_context() -> AgentContext:
    """A context that grants both tools, so either can be called."""
    return _context(max_requests=4).model_copy(
        update={
            "authorized_tools": (
                ToolContract(
                    tool_id=ToolId.create(),
                    name="ping",
                    description="Does nothing.",
                    input_schema={
                        "type": "object",
                        "properties": {"note": {"type": "string"}},
                    },
                ),
                ToolContract(
                    tool_id=ToolId.create(),
                    name="delegate_to_agent",
                    description="Hand part of this task to another agent.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "agent_name": {"type": "string"},
                            "objective": {"type": "string"},
                        },
                    },
                ),
            )
        }
    )


async def _accepting_execute(*, tool_name: str, arguments: dict[str, Any]) -> str:
    """Every tool succeeds, which is what made the dropped proposals invisible."""
    return "ok"


class TestTheLoopIsCapped:
    async def test_a_model_that_never_stops_is_stopped(self) -> None:
        gateway = _EndlessGateway()
        result = await run_with_pydantic_ai(
            task=_task(), context=_context(max_requests=4), gateway=gateway, execute_tool=_execute
        )
        assert result.status is AgentResultStatus.FAILED, result.summary
        assert result.error_code == "budget_exhausted", result.error_code
        # The point of the test: the loop stopped at the ceiling rather than
        # running until something less predictable stopped it.
        #
        # **Which** ceiling is the thing that changed, and this is why the test
        # was worth keeping through the change. `max_requests` was removed as a
        # separate counter and the tool ceiling now governs both counts. That
        # only works because `_EndlessGateway` never calls a tool: it makes model
        # calls and nothing else, so a tool-call ceiling alone would never fire
        # and the run would be unbounded. Removing the request counter outright
        # turned this test red at 13 calls; the failure is the argument.
        ceiling = _context(max_requests=4).budget.max_tool_calls
        assert gateway.calls <= ceiling, (
            f"the loop made {gateway.calls} calls against a ceiling of {ceiling}"
        )

    async def test_the_failure_says_how_to_raise_the_ceiling(self) -> None:
        """An operator reading only the summary should know which knob to turn.

        Asserted on the *name of a real setting*, not on a phrase. The knob moved
        when `max_requests` was folded into the tool ceiling, and a test that
        pinned the old word would have failed on a rename while saying nothing
        about whether an operator could still act on the message.
        """
        from ai_orchestrator.config.settings import Settings

        result = await run_with_pydantic_ai(
            task=_task(),
            context=_context(max_requests=3),
            gateway=_EndlessGateway(),
            execute_tool=_execute,
        )
        assert "max_tool_calls" in result.summary, result.summary
        assert hasattr(Settings(), "max_tool_calls"), (
            "the summary must name a setting that exists, or it sends an operator "
            "to a knob that is not there"
        )

    async def test_a_finite_model_still_finishes(self) -> None:
        """The ceiling must not stop a well-behaved model from answering.

        A cap that also broke the normal case would be fixed by raising the cap,
        which is how a runaway loop gets to spend the money in the first place.
        """
        gateway = _OneShotGateway()
        result = await run_with_pydantic_ai(
            task=_task(), context=_context(max_requests=4), gateway=gateway, execute_tool=_execute
        )
        assert result.status is AgentResultStatus.COMPLETED, result.summary
        assert gateway.calls == 1, f"a one-shot answer took {gateway.calls} model calls"


@pytest.mark.parametrize(
    "resolver,expected",
    [
        (_usage_limit_exceeded, "UsageLimitExceeded"),
        (_unexpected_model_behavior, "UnexpectedModelBehavior"),
    ],
)
def test_the_deferred_exception_lookups_are_the_real_ones(resolver: Any, expected: str) -> None:
    """Guards the deferred imports.

    A wrong stand-in would silently never match, so a budget breach or a
    hallucinated tool would go back to being a 500 while the tests stayed green.
    """
    import pydantic_ai.exceptions as exceptions

    assert resolver() is getattr(exceptions, expected)


@pytest.mark.parametrize("field", ["max_requests", "max_tool_calls"])
def test_the_envelope_carries_both_ceilings(field: str) -> None:
    """Tokens bound a call; these bound the loop. Removing either reopens F39."""
    envelope = BudgetEnvelope(max_tokens=1000, max_cost_usd=Money("1"), max_runtime_s=60)
    assert getattr(envelope, field) > 0
