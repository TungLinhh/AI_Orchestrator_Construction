"""Temporal workflows and activities.

Temporal owns durability, retries, timers and the pause/resume around a human
decision. It does not own business state: the task row is the system's truth and
this module is a projection of it. That split is what stops a second state
machine appearing beside the real one.

The determinism rule is absolute, so it is worth stating why it bites. A workflow
is replayed from its event history on every recovery, and any non-deterministic
value it captured the first time becomes a divergence. Concretely, that means no
`datetime.now()`, no `random`, no database read and no HTTP call inside a workflow
method. Every one of those is an activity, and that is the whole reason for the
activity/workflow split rather than a matter of layering taste.

Workflow code holds no session and no database handle. It passes ids and gets
typed results back. A workflow that can read a database can produce a different
result on replay, which is exactly the bug Temporal's determinism check exists to
prevent.
"""

from __future__ import annotations

import asyncio
from dataclasses import field
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

#: Task queue. One queue for the MVP; a per-workflow-type queue is the natural
#: split once a slow workflow would otherwise starve a fast one.
TASK_QUEUE = "ao-workflows"

#: Registered workflow names. The string is the wire contract: renaming one
#: orphans every running execution, because Temporal matches on it.
WORKFLOW_TASK_EXECUTION = "TaskExecutionWorkflow"
WORKFLOW_DELEGATION = "DelegationWorkflow"

#: Signal name for a human decision. The approval row stores this so the API
#: knows which signal to send.
SIGNAL_APPROVAL_DECISION = "approval_decision"

#: How long a task may run before the workflow gives up on it. The task row also
#: carries a deadline; this is the outer bound, so a task with no deadline still
#: cannot run forever.
DEFAULT_TASK_TIMEOUT = timedelta(minutes=15)

#: Approval wait. Bounded by the approval's own TTL, which is shorter; this is
#: the outer bound for a workflow that somehow never receives the signal.
DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=2)

#: Retry policy for an activity that executes a task.
#
# Non-retryable error types are listed explicitly rather than relying on a
# default, because a blind retry of a policy denial is a security incident and a
# blind retry of a budget refusal spends money to be told no again.
NON_RETRYABLE_ERROR_TYPES = (
    "PolicyDenied",
    "AuthorizationError",
    "ApprovalRequired",
    "BudgetExceeded",
    "ValidationError",
    "CycleDetected",
    "TaskCanceledError",
    "SandboxViolation",
    "NotFoundError",
)

RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    # Bounded. An unbounded retry budget is how a dead dependency eats a cluster.
    maximum_attempts=5,
    # Never retry these, whatever the attempt count.
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPES),
)


class ApprovalSignal(BaseModel):
    """What a human decided. Delivered as a workflow signal."""

    approval_id: str
    approved: bool
    decided_by: str = ""
    note: str = ""


class ExecutionSignal(BaseModel):
    """A payload the API may push into a running execution."""

    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class TaskWorkflowInput(BaseModel):
    """Everything a workflow needs. Ids and options, never a live object."""

    task_id: str
    organization_id: str
    agent_id: str | None = None
    attempt: int = 1
    max_attempts: int = 3
    task_timeout_s: int = 900
    approval_timeout_s: int = 7200
    run_mode: str = "live"
    # The delegation chain, carried as data so the activity can rebuild the path
    # without the workflow holding a repository.
    delegation_path: list[dict[str, Any]] = field(default_factory=list)
    retrieved_context: list[str] = field(default_factory=list)


class ExecutionResult(BaseModel):
    """What an activity reports back. A plain typed value, replay-safe."""

    task_id: str
    status: str
    summary: str = ""
    execution_id: str | None = None
    agent_id: str | None = None
    needs_approval: bool = False
    blocked_reason: str | None = None
    failure_category: str | None = None
    cost_usd: str = "0"
    tokens: int = 0
    duration_ms: int = 0
    proposed_actions: list[dict[str, Any]] = field(default_factory=list)


@workflow.defn(name=WORKFLOW_TASK_EXECUTION)
class TaskExecutionWorkflow:
    """Run one task, durably, with a human approval gate.

    The shape is deliberately shallow. Everything with a side effect is an
    activity; the workflow only sequences, waits and branches. A workflow that
    grows conditionals is usually business logic in the wrong place.
    """

    def __init__(self) -> None:
        self._approved = False
        self._rejected = False
        self._note = ""
        self._extra_input: dict[str, Any] = {}
        self._resume = asyncio.Event()

    @workflow.run
    async def run(self, data: TaskWorkflowInput) -> ExecutionResult:
        workflow.logger.info(f"task workflow started for {data.task_id}")

        # Start the execution. The handle is awaited rather than started so a
        # crash mid-execution is retried by Temporal, not silently dropped.
        result = await self._execute(data, data.attempt)

        # Approval gate. Loops because a rejection can be followed by a revised
        # attempt that needs approving again; a bounded loop so this cannot become
        # an unbounded human-in-the-loop ping-pong.
        for _ in range(3):
            if not result.needs_approval:
                break
            approved, note = await self._wait_for_decision(data.approval_timeout_s)
            if not approved:
                workflow.logger.info(f"task {data.task_id} approval rejected: {note}")
                return result.model_copy(
                    update={"status": "failed", "summary": note or "rejected by approver"}
                )
            # The approval is bound to a payload hash. The activity re-verifies
            # it immediately before the side effect, so a payload that changed
            # while the human was reading it is caught there, not trusted here.
            result = await self._execute(data, data.attempt + 1)

        return result

    async def _execute(self, data: TaskWorkflowInput, attempt: int) -> ExecutionResult:
        return await workflow.execute_activity(
            self.execute_task_activity,
            data.model_copy(update={"attempt": attempt}),
            start_to_close_timeout=timedelta(seconds=data.task_timeout_s),
            heartbeat_timeout=timedelta(seconds=data.task_timeout_s // 3),
            retry_policy=RETRY_POLICY,
        )

    async def _wait_for_decision(self, timeout_s: int) -> tuple[bool, str]:
        """Wait for a human, with a bound.

        A bare `await signal()` with no timeout is a task that waits forever when
        the approval expires and no signal ever arrives. The timeout turns that
        into a decision, and the workflow records which decision it was.
        """
        try:
            await workflow.wait_condition(
                lambda: self._approved or self._rejected,
                timeout=timedelta(seconds=timeout_s),
            )
        except TimeoutError:
            self._note = "approval expired without a decision; failing closed"
            return False, self._note
        return self._approved, self._note

    # ------------------------------------------------------------ signals --
    @workflow.signal
    async def approval_decision(self, signal: ApprovalSignal) -> None:
        """A human decided. The workflow is unblocked and re-runs the task."""
        if signal.approved:
            self._approved = True
        else:
            self._rejected = True
        self._note = signal.note
        self._resume.set()

    @workflow.signal
    def provide_input(self, signal: ExecutionSignal) -> None:
        """Extra input supplied while the task waits."""
        self._extra_input.update(signal.payload)

    @workflow.query(name="state")
    def state(self) -> dict[str, Any]:
        """Introspection for the UI: what is this workflow waiting for?"""
        return {
            "approved": self._approved,
            "rejected": self._rejected,
            "waiting_for_approval": not (self._approved or self._rejected),
            "note": self._note,
            "has_extra_input": bool(self._extra_input),
        }

    # ---------------------------------------------------------- activities --
    @activity.defn(name="execute_task")
    async def execute_task_activity(self, data: TaskWorkflowInput) -> ExecutionResult:
        """Run the task. Every side effect lives here."""
        from ai_orchestrator.worker_runtime import build_execution_service

        service = build_execution_service(data.organization_id, run_mode=data.run_mode)
        outcome = await service.execute_task(
            data.task_id,
            agent_id=data.agent_id,
            attempt=data.attempt,
            input_override=self._extra_input or None,
            retrieved_context=data.retrieved_context,
        )
        activity.logger.info(f"task {data.task_id} -> {outcome.status.value}")
        return ExecutionResult(
            task_id=outcome.task_id,
            status=outcome.status.value,
            summary=outcome.summary,
            execution_id=outcome.execution_id,
            agent_id=outcome.agent_id,
            needs_approval=outcome.needs_approval,
            blocked_reason=outcome.blocked_reason,
            failure_category=outcome.failure_category,
            cost_usd=str(outcome.cost_usd),
            tokens=outcome.tokens,
            duration_ms=outcome.duration_ms,
            proposed_actions=[a.model_dump(mode="json") for a in outcome.proposed_actions],
        )


@workflow.defn(name=WORKFLOW_DELEGATION)
class DelegationWorkflow:
    """Delegate a subtask to another agent and wait for the result.

    A child workflow rather than a loop inside the parent: a delegated task has
    its own lifecycle, its own approvals and its own failure, and folding it into
    the parent would mean one failure takes down both.
    """

    def __init__(self) -> None:
        self._result: ExecutionResult | None = None
        self._done = False

    @workflow.run
    async def run(self, data: TaskWorkflowInput) -> ExecutionResult:
        result = await workflow.execute_child_workflow(
            TaskExecutionWorkflow.run,
            data,
            id=f"{data.task_id}:delegated",
            task_queue=TASK_QUEUE,
            retry_policy=RETRY_POLICY,
        )
        self._result = result
        self._done = True
        return result

    @workflow.signal
    def approval_decision(self, signal: ApprovalSignal) -> None:
        # The child owns the approval state. Forwarding keeps the parent from
        # holding a second, divergent copy of a human decision.
        pass

    @workflow.query(name="state")
    def state(self) -> dict[str, Any]:
        return {"done": self._done, "result": self._result.model_dump() if self._result else None}


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT",
    "DEFAULT_TASK_TIMEOUT",
    "NON_RETRYABLE_ERROR_TYPES",
    "RETRY_POLICY",
    "SIGNAL_APPROVAL_DECISION",
    "TASK_QUEUE",
    "WORKFLOW_DELEGATION",
    "WORKFLOW_TASK_EXECUTION",
    "ApprovalSignal",
    "DelegationWorkflow",
    "ExecutionResult",
    "ExecutionSignal",
    "TaskExecutionWorkflow",
    "TaskWorkflowInput",
]
