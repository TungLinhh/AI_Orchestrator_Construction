"""Coordination lifecycle: an accepted handoff ends the run without another model turn.

Chain-acceptance round 1: the CEO delegated to the Front Office (accepted),
re-requested the same delegation (correctly refused as a duplicate), and then
the acknowledgement model turn failed -- DOTS rejected by the strict parser,
NVIDIA past its 60s candidate deadline -- failing the root AFTER the
delegation had already succeeded. The acknowledgement turn only re-reads what
the backend already authorised, so for a coordinator with a fully accepted
handoff batch the run now returns control without spending it.

Boundaries, all tested below:

* accepted batch ends, with no further model call (the defect);
* refused handoff continues the loop (the model must see the refusal);
* mixed batches continue (the model has not reacted to the other results);
* multi-child fanout in one batch is preserved, never truncated;
* workers keep the acknowledgement turn (their answer is contract-checked);
* a terminal parent never executes its child: the orphan is reported, stays
  assigned, and is excluded from dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import ai_orchestrator.application.pipeline as pipeline_module
from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai
from ai_orchestrator.application.pipeline import (
    _orphan_reason,
    _orphan_step,
    _partition_orphans,
)
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
from ai_orchestrator.tools.registry import ToolResult

pytestmark = pytest.mark.unit

ORG = "org_01m3d5hwxet3x61vjc1ffjyrzh"
AGENT = "agt_01m3d5hwxet3x61vjc1ffjyrzj"
TASK = "tsk_01m3d5hwxet3x61vjc1ffjyrzk"
EXEC = "exe_01m3d5hwxet3x61vjc1ffjyrzm"
CHILD = "tsk_01m3d5hwxet3x61vjc1ffjyrzn"


def _delegate_contract() -> ToolContract:
    return ToolContract(
        tool_id=ToolId.create(),
        name="delegate_to_agent",
        description="Hand part of this task to another agent in the organisation.",
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "objective": {"type": "string"},
            },
            "required": ["agent_name", "objective"],
        },
    )


def _context(**overrides: Any) -> AgentContext:
    base: dict[str, Any] = {
        "actor": Actor(id=AGENT, kind=ActorType.AGENT, organization_id=OrganizationId(ORG)),
        "task": AgentTask(
            task_id=TASK,
            organization_id=OrganizationId(ORG),
            goal="handle the customer matter through the organisation chart",
            task_type=TaskType.COORDINATION,
            execution_id=EXEC,
        ),
        "system_instructions": "You are the chief executive.",
        "organization_id": ORG,
        "budget": BudgetEnvelope(max_tokens=8_000, max_cost_usd=Money("1.00"), max_runtime_s=60),
        "data_classification": DataClassification.INTERNAL,
        "delegate_targets": (("Front Office Agent", "handles customer matters"),),
        "authorized_tools": (_delegate_contract(),),
    }
    base.update(overrides)
    return AgentContext(**base)


def _delegate_call(call_id: str, agent: str = "Front Office Agent") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "delegate_to_agent",
            "arguments": (
                f'{{"agent_name": "{agent}", "objective": "handle matter MOCK-REQ-01 ({call_id})"}}'
            ),
        },
    }


def _tool_turn(*calls: dict[str, Any]) -> GatewayResponse:
    return GatewayResponse(
        text="",
        finish_reason="tool_calls",
        tool_calls=list(calls),
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model_used="scripted",
        provider="test",
    )


def _text_turn(text: str) -> GatewayResponse:
    return GatewayResponse(
        text=text,
        finish_reason="stop",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model_used="scripted",
        provider="test",
    )


class _Scripted:
    """A fake model with a fixed script, counting every call."""

    def __init__(self, *turns: GatewayResponse) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def complete(self, request: Any) -> GatewayResponse:
        self.calls += 1
        if self.calls > len(self._turns):
            raise AssertionError(f"model called a {self.calls}th time, script exhausted")
        return self._turns[self.calls - 1]


async def _accept(*, tool_name: str, arguments: dict[str, Any]) -> Any:
    child = f"{CHILD}-{arguments.get('agent_name', 'x').split()[0]}"
    return ToolResult(
        ok=True,
        output={
            "accepted": True,
            "agent": arguments.get("agent_name"),
            "objective": arguments.get("objective"),
            "child_task_ids": [child],
        },
    )


async def _refuse(*, tool_name: str, arguments: dict[str, Any]) -> Any:
    return ToolResult.failure(
        "DELEGATION_REFUSED",
        "an equivalent task is already active. Do not delegate this work again.",
    )


class TestAcceptedBatchEndsWithoutFurtherModelCall:
    async def test_no_second_turn_after_an_accepted_handoff(self) -> None:
        """The defect: the acknowledgement turn failed a run whose delegation
        had already succeeded. One model call is the whole run now."""
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("acknowledged"))
        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_accept,
        )
        assert result.status is AgentResultStatus.COMPLETED, result.summary
        assert gateway.calls == 1, f"the run spent {gateway.calls} model call(s)"

    async def test_the_report_names_the_child_and_claims_nothing_finished(self) -> None:
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("acknowledged"))
        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_accept,
        )
        assert CHILD in result.summary, result.summary
        assert "still open" in result.summary, result.summary
        assert "complet" not in result.summary.lower(), result.summary

    async def test_proposals_and_usage_survive_the_shortcut(self) -> None:
        """The shortcut must not lose the record of what was asked, nor the
        ledger entry for the call that asked it."""
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("acknowledged"))
        recorded: list[Any] = []

        async def record(response: Any) -> None:
            recorded.append(response)

        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_accept,
            record_usage=record,
        )
        assert [p.tool_name for p in result.follow_up_actions] == ["delegate_to_agent"]
        assert len(recorded) == 1, "the one real model call must still reach the ledger"
        assert recorded[0].model_used

    async def test_fanout_batch_ends_with_every_child_recorded(self) -> None:
        """Two accepted delegations in one batch: both processed, both named,
        still one model call. The shortcut drains the whole batch, never the
        first call of it."""
        gateway = _Scripted(
            _tool_turn(_delegate_call("d1"), _delegate_call("d2", agent="Middle Office Agent")),
            _text_turn("acknowledged"),
        )
        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_accept,
        )
        assert gateway.calls == 1
        assert f"{CHILD}-Front" in result.summary, result.summary
        assert f"{CHILD}-Middle" in result.summary, result.summary
        assert len(result.follow_up_actions) == 2


class TestUnresolvedHandoffsContinueTheLoop:
    async def test_a_refused_handoff_is_shown_to_the_model(self) -> None:
        """The duplicate-delegation refusal must reach the model, exactly as
        the chain run's second turn did -- ending here would hide it."""
        gateway = _Scripted(
            _tool_turn(_delegate_call("d1")),
            _text_turn("Understood, moving on without re-delegating."),
        )
        result = await run_with_pydantic_ai(
            task=_context().task,
            context=_context(),
            gateway=gateway,
            execute_tool=_refuse,
        )
        assert result.status is AgentResultStatus.COMPLETED, result.summary
        assert gateway.calls == 2, f"the run ended after {gateway.calls} model call(s)"
        assert "moving on" in result.summary, result.summary

    async def test_a_mixed_batch_does_not_end_early(self) -> None:
        """An accepted delegation next to another tool's result: the model has
        not reacted to the second result yet, so the loop continues."""
        report = ToolContract(
            tool_id=ToolId.create(),
            name="write_report",
            description="Write a report artifact.",
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        )
        context = _context(authorized_tools=(_delegate_contract(), report))

        async def execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            if tool_name == "delegate_to_agent":
                return await _accept(tool_name=tool_name, arguments=arguments)
            return ToolResult(ok=True, output={"path": "reports/matter.md"})

        gateway = _Scripted(
            _tool_turn(
                _delegate_call("d1"),
                {
                    "id": "w1",
                    "type": "function",
                    "function": {
                        "name": "write_report",
                        "arguments": '{"title": "t", "body": "b"}',
                    },
                },
            ),
            _text_turn("Both done, report written."),
        )
        result = await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=execute
        )
        assert gateway.calls == 2, "a mixed batch must still get its acknowledgement turn"
        assert result.status is AgentResultStatus.COMPLETED, result.summary

    async def test_a_worker_keeps_its_acknowledgement_turn(self) -> None:
        """The shortcut is coordinator-only. A worker's answer is checked
        against its output contract, and skipping its closing turn would skip
        the answer the contract exists to check."""
        context = _context(
            task=AgentTask(
                task_id=TASK,
                organization_id=OrganizationId(ORG),
                goal="draft the answer",
                task_type=TaskType.ANALYSIS,
                execution_id=EXEC,
            ),
            delegate_targets=(),
            delegate_options=(),
        )
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("final answer"))
        result = await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=_accept
        )
        assert gateway.calls == 2, "a worker must keep its closing model turn"
        assert result.status is AgentResultStatus.COMPLETED, result.summary


class TestTerminalParentOrphansHonestly:
    def test_the_reason_names_child_ancestor_status_and_non_execution(self) -> None:
        reason = _orphan_reason(CHILD, {"id": TASK, "status": "failed"})
        assert CHILD in reason
        assert TASK in reason
        assert "failed" in reason
        assert "assigned" in reason
        assert "never" in reason

    def test_the_step_is_blocked_and_leaves_the_task_row_untouched(self) -> None:
        """No execution outcome is fabricated for work that never ran: status
        `blocked` on the step, and the task object itself is unmodified --
        still `assigned`, never completed, never failed."""
        task = SimpleNamespace(
            id=CHILD, title="Handle the matter", status="assigned", parent_task_id=TASK
        )
        step = _orphan_step(task, "Front Office Agent", 1, {"id": TASK, "status": "failed"})
        assert step.task_id == CHILD
        assert step.status == "blocked"
        assert TASK in step.review and "failed" in step.review
        assert "never" in step.review
        assert task.status == "assigned"

    async def test_partition_keeps_orphans_out_of_the_dispatch_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The never-executed guarantee, at the level that provides it: an
        orphaned task is not in the runnable list, so the driver never creates
        an execution for it and the `PreconditionError` site is never reached."""

        class _Repo:
            def __init__(self, session: Any, org: str) -> None:
                pass

            async def terminal_lineage(self, task_id: str) -> dict[str, str] | None:
                if task_id == TASK:
                    return {"id": TASK, "status": "failed"}
                return None

        monkeypatch.setattr(pipeline_module, "TaskRepository", _Repo)
        root = SimpleNamespace(id="tsk_root", parent_task_id=None)
        orphan = SimpleNamespace(id=CHILD, parent_task_id=TASK)
        live_child = SimpleNamespace(id="tsk_live", parent_task_id="tsk_other")
        runnable, orphans = await _partition_orphans(object(), ORG, [root, orphan, live_child])
        assert [t.id for t in runnable] == ["tsk_root", "tsk_live"]
        assert [(t.id, blocker["status"]) for t, blocker in orphans] == [(CHILD, "failed")]


def _accepted_batch_messages(
    call_id: str = "d1",
    agent: str = "Front Office Agent",
    objective: str = "handle matter MOCK-REQ-01",
) -> list[Any]:
    """A transcript whose latest batch is one accepted-looking delegate call.

    The return text claims acceptance; whether the shortcut fires depends only
    on the receipts passed alongside, never on this text.
    """
    from pydantic_ai.messages import ModelRequest as AiModelRequest
    from pydantic_ai.messages import ModelResponse as AiModelResponse
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart
    from pydantic_ai.usage import RequestUsage

    args = {"agent_name": agent, "objective": objective}
    return [
        AiModelResponse(
            parts=[ToolCallPart(tool_name="delegate_to_agent", args=args, tool_call_id=call_id)],
            usage=RequestUsage(),
        ),
        AiModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="delegate_to_agent",
                    content='{"ok": true, "output": {"accepted": true}}',
                    tool_call_id=call_id,
                )
            ]
        ),
    ]


def _accepted_receipt(
    agent: str = "Front Office Agent", objective: str = "handle matter MOCK-REQ-01"
) -> Any:
    from ai_orchestrator.agent_runtime.pydanticai_agent import _ToolReceipt

    return _ToolReceipt.from_result(
        "delegate_to_agent",
        {"agent_name": agent, "objective": objective},
        ToolResult(ok=True, output={"accepted": True, "child_task_ids": [CHILD]}),
    )


class TestReceiptBindingDirect:
    """Acceptance lives in the receipts, not the transcript text."""

    def test_accepted_text_without_receipts_never_matches(self) -> None:
        """Spoofed (or merely unrecorded) acceptance: the transcript claims it
        and no real receipt backs it, so the model stays in the loop."""
        from ai_orchestrator.agent_runtime.pydanticai_agent import _accepted_handoff_summary

        assert _accepted_handoff_summary(_accepted_batch_messages(), receipts=[]) is None

    def test_a_stale_receipt_for_other_arguments_never_matches(self) -> None:
        """A receipt from an earlier batch, for a different call, must not
        vouch for this batch -- the driver scopes receipts per batch, and the
        matcher additionally requires identical arguments."""
        from ai_orchestrator.agent_runtime.pydanticai_agent import _accepted_handoff_summary

        assert (
            _accepted_handoff_summary(
                _accepted_batch_messages(),
                receipts=[_accepted_receipt(objective="something else entirely")],
            )
            is None
        )

    def test_a_matching_batch_receipt_reports_the_handoff(self) -> None:
        from ai_orchestrator.agent_runtime.pydanticai_agent import _accepted_handoff_summary

        summary = _accepted_handoff_summary(
            _accepted_batch_messages(), receipts=[_accepted_receipt()]
        )
        assert summary is not None
        assert CHILD in summary and "Front Office Agent" in summary


class TestFullBatchContractInInstructions:
    def test_coordinators_get_the_full_batch_contract(self) -> None:
        from ai_orchestrator.agent_runtime.pydanticai_agent import _user_prompt

        context = _context()
        text = _user_prompt(context.task, context)
        assert "FULL-BATCH INITIAL-HANDOFF CONTRACT" in text
        assert "FIRST tool batch must contain ALL" in text
        assert "no second round" in text

    def test_workers_do_not_get_the_contract(self) -> None:
        from ai_orchestrator.agent_runtime.pydanticai_agent import _user_prompt

        context = _context(
            task=AgentTask(
                task_id=TASK,
                organization_id=OrganizationId(ORG),
                goal="draft the answer",
                task_type=TaskType.ANALYSIS,
                execution_id=EXEC,
            ),
            delegate_targets=(),
            delegate_options=(),
        )
        assert "FULL-BATCH INITIAL-HANDOFF CONTRACT" not in _user_prompt(context.task, context)


class TestReceiptBinding:
    """Acceptance is read off the real `ToolResult`, never the transcript text.

    A model can write `{"accepted": true}` into a message; it cannot mint the
    receipt object. These tests pin the strictness of that binding."""

    async def test_an_untrusted_acceptance_does_not_end_the_run(self) -> None:
        async def execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            return ToolResult(
                ok=True,
                output={
                    "accepted": True,
                    "child_task_ids": [CHILD],
                    "untrusted": True,
                },
            )

        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("ack"))
        result = await run_with_pydantic_ai(
            task=_context().task, context=_context(), gateway=gateway, execute_tool=execute
        )
        assert gateway.calls == 2, "flagged content must stay with the model"
        assert result.status is AgentResultStatus.COMPLETED, result.summary

    async def test_acceptance_without_child_ids_does_not_end_the_run(self) -> None:
        async def execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            return ToolResult(ok=True, output={"accepted": True, "child_task_ids": []})

        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("ack"))
        await run_with_pydantic_ai(
            task=_context().task, context=_context(), gateway=gateway, execute_tool=execute
        )
        assert gateway.calls == 2, "no child ids means no proof a task was created"


def _coordination_task(task_input: dict[str, Any]) -> AgentTask:
    return AgentTask(
        task_id=TASK,
        organization_id=OrganizationId(ORG),
        goal="handle the customer matter through the organisation chart",
        task_type=TaskType.COORDINATION,
        execution_id=EXEC,
        input=task_input,
    )


class TestDeclaredOwnerCoverage:
    async def test_a_covered_declared_owner_ends_the_run(self) -> None:
        context = _context(task=_coordination_task({"owning_office": "Front Office Agent"}))
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("ack"))
        result = await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=_accept
        )
        assert gateway.calls == 1
        assert result.status is AgentResultStatus.COMPLETED, result.summary

    async def test_a_missed_declared_owner_continues_the_loop(self) -> None:
        """Delegated, accepted, but to nobody the input declared: the batch is
        not the complete handoff, so the model still closes the turn."""
        context = _context(task=_coordination_task({"owning_office": "Front Office Agent"}))
        gateway = _Scripted(
            _tool_turn(_delegate_call("d1", agent="Back Office Agent")),
            _text_turn("ack"),
        )
        await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=_accept
        )
        assert gateway.calls == 2

    async def test_a_slug_matches_its_display_name(self) -> None:
        context = _context(task=_coordination_task({"owning_department": "front-office"}))
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("ack"))
        await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=_accept
        )
        assert gateway.calls == 1

    async def test_an_unverifiable_declaration_disables_the_shortcut(self) -> None:
        """Declared but uninterpretable coverage cannot be verified, so the run
        keeps its acknowledgement turn rather than guessing."""
        context = _context(task=_coordination_task({"owning_office": {"unit": 1}}))
        gateway = _Scripted(_tool_turn(_delegate_call("d1")), _text_turn("ack"))
        await run_with_pydantic_ai(
            task=context.task, context=context, gateway=gateway, execute_tool=_accept
        )
        assert gateway.calls == 2

    def test_expected_owners_parsing(self) -> None:
        from ai_orchestrator.agent_runtime.pydanticai_agent import _expected_owners

        assert _expected_owners({}) == ()
        assert _expected_owners(None) == ()
        assert _expected_owners({"owning_office": "A", "owning_department": ["B", "C"]}) == (
            "A",
            "B",
            "C",
        )
        assert _expected_owners({"owning_office": ""}) == ()
        assert _expected_owners({"owning_office": {"unit": 1}}) is None


class TestRepeatDelegationStaysWithModel:
    async def test_a_retry_after_refusal_gets_its_acknowledgement_turn(self) -> None:
        """The chain's duplicate shape: refused once, accepted on re-request.
        The second batch repeats an agent with history, so it is the model's
        turn to close, not a fresh handoff to end on."""
        seen: list[str] = []

        async def execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            seen.append(str(arguments.get("agent_name")))
            if len(seen) == 1:
                return await _refuse(tool_name=tool_name, arguments=arguments)
            return await _accept(tool_name=tool_name, arguments=arguments)

        gateway = _Scripted(
            _tool_turn(_delegate_call("d1")),
            _tool_turn(_delegate_call("d2")),
            _text_turn("ack"),
        )
        result = await run_with_pydantic_ai(
            task=_context().task, context=_context(), gateway=gateway, execute_tool=execute
        )
        assert seen == ["Front Office Agent", "Front Office Agent"]
        assert gateway.calls == 3, f"the repeat ended the run after {gateway.calls} call(s)"
        assert result.status is AgentResultStatus.COMPLETED, result.summary


class TestClaimedStepNarrowGuard:
    """The race window: a parent settling after dispatch. `_execute_claimed_step`
    rereads terminal lineage before attempting execution."""

    def _fakes(self, monkeypatch: pytest.MonkeyPatch, *, blocker: Any) -> Any:
        task = SimpleNamespace(
            id=CHILD,
            title="Handle the matter",
            status="assigned",
            parent_task_id=TASK,
            owner_agent_id=AGENT,
        )

        class _Session:
            pass

        class _SessionCM:
            async def __aenter__(self) -> Any:
                return _Session()

            async def __aexit__(self, *args: Any) -> bool:
                return False

        class _Database:
            def tenant_session(self, org: str) -> Any:
                return _SessionCM()

        class _Repo:
            def __init__(self, session: Any, org: str) -> None:
                pass

            async def get(self, task_id: str) -> Any:
                return task

            async def terminal_lineage(self, task_id: str) -> Any:
                return blocker

        async def _owner(session: Any, org: str, agent_id: str) -> str:
            return "Front Office Agent"

        monkeypatch.setattr(pipeline_module, "TaskRepository", _Repo)
        monkeypatch.setattr(pipeline_module, "_owner_name", _owner)
        return _Database(), task

    async def test_confirmed_terminal_ancestor_reports_without_executing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ai_orchestrator.application.task_attempt as attempt_module
        from ai_orchestrator.domain.enums import RunMode

        database, task = self._fakes(monkeypatch, blocker={"id": TASK, "status": "failed"})

        class _BombRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("no execution may be attempted under a terminal parent")

        monkeypatch.setattr(attempt_module, "TaskAttemptRunner", _BombRunner)
        step = await pipeline_module._execute_claimed_step(
            database,  # type: ignore[arg-type]
            ORG,
            CHILD,
            1,
            run_mode=RunMode.LIVE,
            auto_approve=False,
        )
        assert step is not None
        assert step.orphan is True
        assert step.status == "blocked"
        assert TASK in step.review and "failed" in step.review
        assert task.status == "assigned"

    async def test_an_unrelated_refusal_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ai_orchestrator.application.task_attempt as attempt_module
        from ai_orchestrator.domain.enums import RunMode
        from ai_orchestrator.domain.errors import PreconditionError

        database, _ = self._fakes(monkeypatch, blocker=None)

        class _RefusingRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def execute_task(self, task_id: str, **kwargs: Any) -> Any:
                raise PreconditionError("something else entirely")

        monkeypatch.setattr(attempt_module, "TaskAttemptRunner", _RefusingRunner)
        with pytest.raises(PreconditionError):
            await pipeline_module._execute_claimed_step(
                database,  # type: ignore[arg-type]
                ORG,
                CHILD,
                1,
                run_mode=RunMode.LIVE,
                auto_approve=False,
            )


class TestSharedSessionBatchSerializes:
    """One session serves this run's tools, so a batch must not overlap them.

    Chain-acceptance round 2: three model turns succeeded, then the run died
    with `another operation is in progress` on `SELECT ... FOR UPDATE OF tasks`
    and a poisoned `prepared` transaction -- and zero delegations were
    persisted. The framework's default runs a batch's tools in parallel, so two
    `delegate_to_agent` calls issued their ownership checkpoints concurrently
    on the one `TaskExecutionService` session; the rollback took the children
    with it. The driver now forces sequential tool execution: order is the
    only thing that changes, every call still runs.
    """

    async def test_a_two_call_batch_never_overlaps_the_executor(self) -> None:
        import asyncio

        inflight = 0
        peak = 0

        async def execute(*, tool_name: str, arguments: dict[str, Any]) -> Any:
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            try:
                await asyncio.sleep(0.01)
                return await _accept(tool_name=tool_name, arguments=arguments)
            finally:
                inflight -= 1

        gateway = _Scripted(
            _tool_turn(_delegate_call("d1"), _delegate_call("d2", agent="Middle Office Agent")),
        )
        result = await run_with_pydantic_ai(
            task=_context().task, context=_context(), gateway=gateway, execute_tool=execute
        )
        assert peak == 1, f"two tool calls overlapped on the shared session (peak {peak})"
        assert gateway.calls == 1
        assert len(result.follow_up_actions) == 2
        assert f"{CHILD}-Front" in result.summary, result.summary
        assert f"{CHILD}-Middle" in result.summary, result.summary
