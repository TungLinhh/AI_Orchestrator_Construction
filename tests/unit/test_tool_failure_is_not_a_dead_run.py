"""A refused tool must not kill the agent that called it.

Two live child agents died with:

    Tool 'write_report' exceeded max retries count of 0

`max_retries=0` on every tool is correct — the gateway owns retry policy, and a
framework retry would re-run a side-effecting tool with no regard for whether it
already ran. But it means a *refusal* is also fatal, because PydanticAI treats a
raised tool error as a model mistake and, having no retries left, aborts the run.

The refusal is the platform working. The model was never shown it, and never got
the chance to do something else. Two agents in a row produced no output at all
because of it.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
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
from ai_orchestrator.tools.registry import ToolResult

pytestmark = pytest.mark.unit

ORG = "org_01m3d5hwxet3x61vjc1ffjyrzh"
AGENT = "agt_01m3d5hwxet3x61vjc1ffjyrzj"
TASK = "tsk_01m3d5hwxet3x61vjc1ffjyrzk"
EXEC = "exe_01m3d5hwxet3x61vjc1ffjyrzm"


class _CallsWriteReportThenAnswers:
    """Asks for the report once, then writes prose.

    The shape of a real run: call the tool, read what came back, decide.
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
                        "id": "w1",
                        "type": "function",
                        "function": {
                            "name": "write_report",
                            "arguments": '{"title": "Job Description", "body": "A senior role."}',
                        },
                    }
                ],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model_used="scripted",
                provider="test",
            )
        return GatewayResponse(
            text="The write was refused, so here is the description inline instead.",
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
            goal="draft a job description",
            task_type=TaskType.ANALYSIS,
            execution_id=EXEC,
        ),
        system_instructions="You draft documents.",
        organization_id=ORG,
        budget=BudgetEnvelope(max_tokens=8_000, max_cost_usd=Money("1.00"), max_runtime_s=60),
        data_classification=DataClassification.INTERNAL,
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


def _task() -> AgentTask:
    return _context().task


async def _refuse(*, tool_name: str, arguments: dict[str, Any]) -> Any:
    return ToolResult.failure("PATH_ESCAPE", "outside the tenant")


class TestARefusedToolIsRecoverable:
    async def test_a_refused_write_does_not_kill_the_run(self) -> None:
        """The defect. Two live child agents produced no output because of this."""
        result = await run_with_pydantic_ai(
            task=_task(),
            context=_context(),
            gateway=_CallsWriteReportThenAnswers(),
            execute_tool=_refuse,
        )
        assert result.status is AgentResultStatus.COMPLETED, result.summary
        assert "refused" in result.summary, result.summary

    async def test_the_run_actually_reached_a_second_turn(self) -> None:
        """Proving the first assertion honestly.

        "The summary mentions refusing" would also pass if the run had ended on the
        first turn and the model had simply predicted the outcome. The gateway
        being asked twice is the evidence that the tool result was returned rather
        than raised.
        """
        gateway = _CallsWriteReportThenAnswers()
        await run_with_pydantic_ai(
            task=_task(), context=_context(), gateway=gateway, execute_tool=_refuse
        )
        assert gateway.calls == 2, f"the run ended after {gateway.calls} model call(s)"


class TestTheRefusalReachesTheModelIntact:
    async def test_a_refusal_is_serialised_with_its_real_field_names(self) -> None:
        """The model can only adapt to a refusal it can read.

        Serialised with `error_kind` and `error_message`, which are `ToolResult`'s
        actual fields. An earlier version read `error_code` and `message`, which do
        not exist, so every refusal reached the model as
        `{"ok": true, "output": null, "error": null, "message": null}` — a success
        with no output. A tool that refuses and reports success is worse than one
        that crashes, because the model acts on it.
        """
        rendered = await _rendered_tool_return(_refuse)
        assert '"ok": false' in rendered, rendered
        assert "PATH_ESCAPE" in rendered, rendered
        assert "outside the tenant" in rendered, rendered

    async def test_a_success_is_not_reported_as_a_failure(self) -> None:
        async def succeed(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            return ToolResult(ok=True, output={"path": "reports/job.md"})

        rendered = await _rendered_tool_return(succeed)
        assert '"ok": true' in rendered, rendered
        assert "null" not in rendered.split('"error"')[0][-40:], rendered


async def _rendered_tool_return(execute: Any) -> str:
    """One tool call, rendered exactly as the model receives it.

    Goes through the real bridge helper rather than a copy of it, so a change to
    the serialisation cannot leave this test asserting on stale text.
    """
    from ai_orchestrator.agent_runtime.pydanticai_agent import _runner

    contract = _context().authorized_tools[0]
    return await _runner(contract, execute)({"title": "t", "body": "b"})
