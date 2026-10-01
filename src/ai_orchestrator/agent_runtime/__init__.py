"""Agent runtime adapters.

The seam that makes the framework replaceable. Two implementations ship:

  PydanticAIRuntime   the primary one
  ScriptedRuntime     deterministic, offline, zero-cost

Both are held to the same contract, and `tests/unit/test_runtime_swap.py`
asserts the swap changes nothing outside this package. A runtime is handed an
`AgentContext` and returns an `AgentResult`; it is not given the ability to
look up its own authority, its own tools, or the organization graph.

The `ScriptedRuntime` exists so CI can exercise the entire pipeline — context
assembly, policy, tools, budget, persistence — with no network and no spend. It
is not a mock in the "returns canned success" sense: it plans, proposes actions,
consumes budget, and produces real typed results from a deterministic policy
function. That is what makes a test with it worth running.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from ai_orchestrator.domain.contracts import (
    ActionProposal,
    AgentContext,
    AgentResult,
    AgentResultStatus,
    AgentRuntime,
    RuntimeCapabilities,
)


@runtime_checkable
class RuntimeFactory(Protocol):
    """Builds a runtime for a context.

    A factory rather than a global registry, so the runtime choice is a
    constructor argument and a test can inject a different one without touching
    global state.
    """

    def __call__(self, context: AgentContext) -> AgentRuntime: ...


class PydanticAIRuntime:
    """Primary runtime, backed by PydanticAI.

    The adapter is intentionally thin. Everything the run needs — instructions,
    tools, memory, budget, deadline — is already in the `AgentContext`, assembled
    and authorised by the control plane. The runtime's job is to reason and to
    *propose*; it never performs a side effect itself.

    That separation is the reason the adapter is small. A runtime that also owned
    tool execution, memory access and budget accounting would be a second control
    plane, and swapping frameworks would mean swapping governance.
    """

    name = "pydantic_ai"

    def __init__(self, *, model: Any = None, settings_deferral: float = 0.0) -> None:
        self._model = model
        self._settings_deferral = settings_deferral

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            name=self.name,
            streaming=True,
            tool_calling=True,
            structured_output=True,
            subagents=True,
            checkpoints=False,
            maturity="foundation",
        )

    async def execute(
        self,
        task: Any,
        context: AgentContext,
        *,
        record_usage: Callable[[Any], Awaitable[None]] | None = None,
        execute_tool: Callable[..., Awaitable[Any]] | None = None,
    ) -> AgentResult:
        """Run one task.

        Implemented in `ai_orchestrator.agent_runtime.pydanticai_agent`, which
        owns the PydanticAI-specific details. This method exists so the class
        satisfies the `AgentRuntime` protocol regardless of whether the optional
        dependency is installed.
        """
        from ai_orchestrator.agent_runtime.pydanticai_agent import run_with_pydantic_ai

        result: AgentResult = await run_with_pydantic_ai(
            task,
            context,
            model=self._model,
            # Forwarded, not accepted and dropped. Dropping it means the model gets
            # a tool it can see and cannot run, and the run fails with
            # `TypeError: 'NoneType' object is not callable` from inside the
            # tool body — which reads like a bug in the model call rather than a
            # dropped argument.
            record_usage=record_usage,
            execute_tool=execute_tool,
        )
        return result


class ScriptedRuntime:
    """A deterministic runtime for tests, CI and simulations.

    Behaviour is driven by a `planner` callable rather than by a lookup table, so
    a test can express "this agent should delegate" or "this agent should call a
    tool" and get exactly that, with the surrounding machinery still running for
    real.

    Cost is reported as a fixed amount per call so budget accounting, the budget
    ledger and the overspend path all get exercised without a provider.
    """

    name = "scripted"

    def __init__(
        self,
        planner: Any = None,
        *,
        simulated_input_tokens: int = 120,
        simulated_output_tokens: int = 80,
        simulated_cost_usd: float = 0.001,
    ) -> None:
        # Default planner: complete the task, which is the right default for the
        # "does the rest of the platform work" question that most tests ask.
        self._planner = planner or _default_planner
        self._input_tokens = simulated_input_tokens
        self._output_tokens = simulated_output_tokens
        self._cost = simulated_cost_usd

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            name=self.name,
            streaming=False,
            tool_calling=True,
            structured_output=True,
            subagents=False,
            checkpoints=False,
            maturity="experimental",
        )

    async def execute(
        self,
        task: Any,
        context: AgentContext,
        *,
        record_usage: Callable[[Any], Awaitable[None]] | None = None,
        execute_tool: Callable[..., Awaitable[Any]] | None = None,
    ) -> AgentResult:
        proposals = self._planner(context)
        if proposals is None:
            proposals = []
        proposals = list(proposals)

        needs_approval = any(
            p.kind in {"tool_call", "delegate", "spawn_subagent"} for p in proposals
        )
        status = AgentResultStatus.NEEDS_APPROVAL if needs_approval else AgentResultStatus.COMPLETED

        from ai_orchestrator.domain.budget import Money, TokenUsage

        return AgentResult(
            status=status,
            summary=f"scripted runtime handled: {context.task.goal}",
            execution_id=context.task.execution_id,
            task_id=context.task.task_id,
            follow_up_actions=tuple(proposals),
            usage=TokenUsage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
            cost_usd=Money(str(self._cost)),
            model_used="scripted/deterministic",
            output={"scripted": True, "proposal_count": len(proposals)},
        )


def _default_planner(context: AgentContext) -> list[ActionProposal]:
    return []


class NullRuntime:
    """Returns a typed failure without attempting anything.

    Used when an agent has no usable runtime configured. It exists so the caller
    gets an `AgentResult` with a real error kind instead of an exception that
    looks like a platform bug — the distinction matters when triaging a failed
    task, and a stack trace cannot tell them apart.
    """

    name = "null"

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(name=self.name, tool_calling=False, structured_output=False)

    async def execute(
        self,
        task: Any,
        context: AgentContext,
        *,
        record_usage: Callable[[Any], Awaitable[None]] | None = None,
        execute_tool: Callable[..., Awaitable[Any]] | None = None,
    ) -> AgentResult:
        from ai_orchestrator.domain.budget import Money, TokenUsage

        return AgentResult(
            status=AgentResultStatus.FAILED,
            summary=(
                f"no runtime adapter is available for "
                f"{context.actor.display_name or context.task.task_id}"
            ),
            execution_id=context.task.execution_id,
            task_id=context.task.task_id,
            usage=TokenUsage(),
            cost_usd=Money("0"),
            error_code="RUNTIME_UNAVAILABLE",
        )


__all__ = [
    "NullRuntime",
    "PydanticAIRuntime",
    "RuntimeFactory",
    "ScriptedRuntime",
]
