"""A model that calls a tool wrongly must be told, not killed.

Three live runs have now died with:

    Tool 'write_report' exceeded max retries count of 0

`max_retries=0` is correct for a tool *handler* — the gateway owns retry policy, and
a framework retry would re-run a side-effecting tool with no regard for whether it
had already run. But `max_retries` also governs **argument validation**, and that is
a different thing entirely: a model that omits `body` has not run any code. The
handler was never entered, so there is no side effect to protect and nothing to
double-execute. Retrying that is free and correct.

The conflation is the bug. One knob is doing two jobs, and setting it for the
dangerous job also disabled the harmless one — so a model that mis-spelled an
argument took down a run that could have recovered on the next turn.

The second property is that a refusal the platform *did* produce reaches the model
as text. `tests/unit/test_tool_failure_is_not_a_dead_run.py` covers the refusal
path; this covers the argument path, which never reaches the handler at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
from ai_orchestrator.domain.budget import Money, TokenUsage
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


class _MistakesTheArgumentsThenAnswers:
    """Calls `write_report` with no `body`, then answers in prose.

    A model argument mistake: validation rejects the call before the handler runs,
    so no artifact is written and nothing needs undoing.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.scripts: list[list[dict[str, Any]]] = []

    async def complete(self, request: Any) -> GatewayResponse:
        self.calls += 1
        if self.calls == 1:
            return GatewayResponse(
                text="",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "w1",
                        "type": "function",
                        "function": {
                            "name": "write_report",
                            "arguments": '{"title": "only a title"}',
                        },
                    }
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model_used="scripted",
                provider="test",
            )
        self.scripts.append([getattr(m, "parts", []) for m in getattr(request, "messages", [])])
        return GatewayResponse(
            text="I left the body out; here is the plan inline instead.",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model_used="scripted",
            provider="test",
        )


def _context() -> AgentContext:
    return AgentContext(
        actor=Actor(id=AGENT, kind=ActorType.AGENT, organization_id=OrganizationId(ORG)),
        task=AgentTask(
            task_id=TASK,
            organization_id=OrganizationId(ORG),
            goal="write a training plan",
            task_type=TaskType.ANALYSIS,
            execution_id=EXEC,
        ),
        system_instructions="You write plans.",
        organization_id=ORG,
        budget=BudgetEnvelope(max_tokens=8_000, max_cost_usd=Money("1.00"), max_runtime_s=60),
        data_classification=DataClassification.PUBLIC,
        authorized_tools=(
            ToolContract(
                tool_id=ToolId.create(),
                name="write_report",
                description="Write a report artifact.",
                input_schema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title", "body"],
                },
            ),
        ),
    )


async def _execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
    from ai_orchestrator.tools.registry import ToolResult

    return ToolResult(ok=True, output={"written": True})


class TestAMistakenArgumentIsRecoverable:
    async def test_a_missing_required_argument_does_not_kill_the_run(self) -> None:
        """The defect. Three live runs died here."""
        gateway = _MistakesTheArgumentsThenAnswers()
        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_execute,
        )
        assert result.status is AgentResultStatus.COMPLETED, result.summary

    async def test_the_run_reaches_a_second_turn(self) -> None:
        """Proving the first assertion honestly.

        Asserting the summary alone would also pass if the run had ended on turn
        one and the model had predicted its own recovery. The gateway being asked
        twice is the evidence.
        """
        gateway = _MistakesTheArgumentsThenAnswers()
        await run_with_pydantic_ai(
            task=_context().task, context=_context(), gateway=gateway, execute_tool=_execute
        )
        assert gateway.calls == 2, f"the run ended after {gateway.calls} model call(s)"

    async def test_the_handler_is_never_entered_for_an_invalid_call(self) -> None:
        """The property that makes the retry safe.

        This is *why* retrying a validation failure is acceptable while retrying a
        handler failure is not: validation happens before the handler, so there is
        no side effect to double.
        """
        entered: list[str] = []

        async def record(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            from ai_orchestrator.tools.registry import ToolResult

            entered.append(tool_name)
            return ToolResult(ok=True, output={})

        await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=_MistakesTheArgumentsThenAnswers(),
            execute_tool=record,
        )
        assert entered == [], (
            f"the handler ran for an invalid call: {entered} — a retry of that "
            "would be a retry of a side effect"
        )
