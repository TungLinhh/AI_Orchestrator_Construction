"""State machines.

A state machine that is only checked at the API layer is not a state machine,
it is a suggestion. These tests assert the legal transition sets directly, so an
edit that quietly permits `completed -> running` fails here.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.enums import (
    AgentLifecycleStatus,
    ApprovalStatus,
    DelegationStatus,
    SubagentStatus,
    TaskStatus,
)
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.domain.state_machines import (
    TASK_TRANSITIONS,
    Transition,
    allowed_task_transitions,
    can_transition_approval,
    can_transition_task,
    is_terminal_task,
    next_agent_status,
    next_approval_status,
    next_delegation_status,
    next_subagent_status,
    next_task_status,
)

ALL_TASK_STATUSES = list(TaskStatus)


class TestTaskMachine:
    def test_happy_path(self) -> None:
        assert next_task_status(TaskStatus.CREATED, Transition.ASSIGN) is TaskStatus.ASSIGNED
        assert next_task_status(TaskStatus.ASSIGNED, Transition.BEGIN_WORK) is TaskStatus.RUNNING
        assert next_task_status(TaskStatus.RUNNING, Transition.COMPLETE) is TaskStatus.COMPLETED

    def test_approval_round_trip(self) -> None:
        """Acceptance scenario 4: pause, wait, resume."""
        assert (
            next_task_status(TaskStatus.RUNNING, Transition.REQUEST_APPROVAL)
            is TaskStatus.WAITING_FOR_APPROVAL
        )
        assert (
            next_task_status(TaskStatus.WAITING_FOR_APPROVAL, Transition.APPROVAL_GRANTED)
            is TaskStatus.RUNNING
        )

    def test_rejected_approval_fails_the_task(self) -> None:
        assert (
            next_task_status(TaskStatus.WAITING_FOR_APPROVAL, Transition.APPROVAL_REJECTED)
            is TaskStatus.FAILED
        )

    def test_blocked_task_can_be_unblocked(self) -> None:
        assert next_task_status(TaskStatus.RUNNING, Transition.BLOCK) is TaskStatus.BLOCKED
        assert next_task_status(TaskStatus.BLOCKED, Transition.UNBLOCK) is TaskStatus.RUNNING

    @pytest.mark.parametrize(
        "terminal",
        [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.EXPIRED],
    )
    @pytest.mark.parametrize("event", list(Transition))
    def test_terminal_states_are_absorbing(self, terminal: TaskStatus, event: Transition) -> None:
        """Nothing leaves a terminal state. Retrying a failed task is a new task;
        mutating the row would make the audit trail lie."""
        assert is_terminal_task(terminal)
        with pytest.raises(PreconditionError):
            next_task_status(terminal, event)

    @pytest.mark.parametrize("current", ALL_TASK_STATUSES)
    @pytest.mark.parametrize("event", list(Transition))
    def test_every_transition_is_declared_in_the_table(
        self, current: TaskStatus, event: Transition
    ) -> None:
        """The function and the data cannot drift apart."""
        declared = event in TASK_TRANSITIONS.get(current, {})
        try:
            next_task_status(current, event)
            applied = True
        except PreconditionError:
            applied = False
        assert declared == applied

    def test_illegal_jump_names_both_states(self) -> None:
        """The error message is the operator's only clue."""
        with pytest.raises(PreconditionError) as exc:
            next_task_status(TaskStatus.CREATED, Transition.COMPLETE)
        message = str(exc.value.message)
        assert "created" in message
        assert "complete" in message

    def test_created_cannot_jump_straight_to_running(self) -> None:
        """A task must be assigned before work starts; that is the ownership record."""
        assert not can_transition_task(TaskStatus.CREATED, Transition.BEGIN_WORK)
        with pytest.raises(PreconditionError):
            next_task_status(TaskStatus.CREATED, Transition.BEGIN_WORK)

    def test_reassignment_is_allowed_but_completion_is_not(self) -> None:
        assert next_task_status(TaskStatus.ASSIGNED, Transition.ASSIGN) is TaskStatus.ASSIGNED
        assert not can_transition_task(TaskStatus.COMPLETED, Transition.ASSIGN)

    def test_cancellation_is_available_from_live_states_only(self) -> None:
        for status in (
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
            TaskStatus.ASSIGNED,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
        ):
            assert Transition.CANCEL in allowed_task_transitions(status), status
        assert Transition.CANCEL not in allowed_task_transitions(TaskStatus.COMPLETED)

    def test_every_live_state_can_reach_a_terminal_state(self) -> None:
        """No state may be a dead end. This is the anti-deadlock property."""
        reachable = {s for s in TaskStatus if not is_terminal_task(s)}
        for status in reachable:
            targets = set(TASK_TRANSITIONS.get(status, {}).values())
            assert targets & {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.EXPIRED,
            }, f"{status} can only reach {targets}, none of them terminal"


class TestAgentLifecycle:
    def test_draft_to_active_requires_provisioning(self) -> None:
        assert next_agent_status(AgentLifecycleStatus.DRAFT, Transition.ACTIVATE) is (
            AgentLifecycleStatus.PROVISIONING
        )
        assert next_agent_status(AgentLifecycleStatus.PROVISIONING, Transition.ACTIVATE) is (
            AgentLifecycleStatus.ACTIVE
        )

    def test_failed_provisioning_returns_to_draft(self) -> None:
        assert next_agent_status(AgentLifecycleStatus.PROVISIONING, Transition.FAIL) is (
            AgentLifecycleStatus.DRAFT
        )

    def test_suspended_resumes_to_paused_not_active(self) -> None:
        """A suspended agent must be reviewed before it works again."""
        assert next_agent_status(AgentLifecycleStatus.SUSPENDED, Transition.RESUME) is (
            AgentLifecycleStatus.PAUSED
        )

    def test_retired_is_terminal(self) -> None:
        for event in Transition:
            with pytest.raises(PreconditionError):
                next_agent_status(AgentLifecycleStatus.RETIRED, event)

    def test_no_delete_state_exists(self) -> None:
        """Audit history must outlive the entity; there is no `deleted`."""
        assert not hasattr(AgentLifecycleStatus, "DELETED")
        assert AgentLifecycleStatus.RETIRED.value == "retired"


class TestApprovalMachine:
    def test_pending_can_be_decided(self) -> None:
        assert next_approval_status(ApprovalStatus.PENDING, ApprovalStatus.APPROVED) is (
            ApprovalStatus.APPROVED
        )
        assert next_approval_status(ApprovalStatus.PENDING, ApprovalStatus.REJECTED) is (
            ApprovalStatus.REJECTED
        )

    def test_rejection_is_final(self) -> None:
        assert not can_transition_approval(ApprovalStatus.REJECTED, ApprovalStatus.APPROVED)
        with pytest.raises(PreconditionError):
            next_approval_status(ApprovalStatus.REJECTED, ApprovalStatus.APPROVED)

    def test_expired_is_final(self) -> None:
        assert not can_transition_approval(ApprovalStatus.EXPIRED, ApprovalStatus.APPROVED)

    def test_needs_information_can_return_to_pending(self) -> None:
        assert (
            next_approval_status(ApprovalStatus.NEEDS_INFORMATION, ApprovalStatus.PENDING)
            is ApprovalStatus.PENDING
        )

    def test_approved_can_only_be_cancelled(self) -> None:
        assert can_transition_approval(ApprovalStatus.APPROVED, ApprovalStatus.CANCELED)
        assert not can_transition_approval(ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)


class TestDelegationAndSubagentMachines:
    def test_delegation_happy_path(self) -> None:
        assert next_delegation_status(DelegationStatus.PROPOSED, Transition.ACCEPT) is (
            DelegationStatus.ACCEPTED
        )
        assert next_delegation_status(DelegationStatus.ACCEPTED, Transition.BEGIN_WORK) is (
            DelegationStatus.IN_PROGRESS
        )
        assert next_delegation_status(DelegationStatus.IN_PROGRESS, Transition.COMPLETE) is (
            DelegationStatus.COMPLETED
        )

    def test_accepted_delegation_cannot_be_completed_directly(self) -> None:
        with pytest.raises(PreconditionError):
            next_delegation_status(DelegationStatus.ACCEPTED, Transition.COMPLETE)

    def test_subagent_expiry(self) -> None:
        """A subagent is time-bounded by construction."""
        assert next_subagent_status(SubagentStatus.RUNNING, Transition.EXPIRE) is (
            SubagentStatus.EXPIRED
        )

    def test_completed_subagent_cannot_run_again(self) -> None:
        with pytest.raises(PreconditionError):
            next_subagent_status(SubagentStatus.COMPLETED, Transition.BEGIN_WORK)
