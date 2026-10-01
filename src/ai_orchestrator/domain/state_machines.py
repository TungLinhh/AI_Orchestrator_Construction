"""State machines.

Canonical state is a column, never inferred from logs, never inferred from a
chat transcript. The legal transitions live here as data so that:

  * the transition map can be asserted against in a unit test;
  * the API, the workflow and the CLI all enforce the same rules;
  * an illegal jump is a `PreconditionError` with a message naming both states.

Each machine is a pure function of (current, event) -> next | None. No I/O, no
database, no clock. That makes the whole thing testable without a fixture.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from ai_orchestrator.domain.enums import (
    AgentLifecycleStatus,
    ApprovalStatus,
    DelegationStatus,
    SubagentStatus,
    TaskStatus,
)
from ai_orchestrator.domain.errors import PreconditionError


class Transition(StrEnum):
    """Events that may drive a state machine forward."""

    START = "start"
    ASSIGN = "assign"
    BEGIN_WORK = "begin_work"
    COMPLETE = "complete"
    FAIL = "fail"
    BLOCK = "block"
    UNBLOCK = "unblock"
    REQUEST_INPUT = "request_input"
    PROVIDE_INPUT = "provide_input"
    REQUEST_APPROVAL = "request_approval"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    CANCEL = "cancel"
    EXPIRE = "expire"
    ACTIVATE = "activate"
    PAUSE = "pause"
    RESUME = "resume"
    DEGRADE = "degrade"
    SUSPEND = "suspend"
    RETIRE = "retire"
    ACCEPT = "accept"
    REJECT = "reject"
    SPAWN = "spawn"
    REPORT_PROGRESS = "report_progress"


# ------------------------------------------------------------- task machine --
# Terminal states map to `{}`: nothing leaves them. Retrying a FAILED task is
# a *new* task (linked via task_dependencies), because reusing the row would
# make the audit trail lie about what was attempted.
TASK_TRANSITIONS: Final[Mapping[TaskStatus, Mapping[Transition, TaskStatus]]] = {
    TaskStatus.CREATED: {
        Transition.ASSIGN: TaskStatus.ASSIGNED,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
        Transition.FAIL: TaskStatus.FAILED,
    },
    TaskStatus.QUEUED: {
        Transition.ASSIGN: TaskStatus.ASSIGNED,
        Transition.BEGIN_WORK: TaskStatus.RUNNING,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
        Transition.FAIL: TaskStatus.FAILED,
    },
    TaskStatus.ASSIGNED: {
        Transition.BEGIN_WORK: TaskStatus.RUNNING,
        Transition.ASSIGN: TaskStatus.ASSIGNED,  # reassignment
        Transition.BLOCK: TaskStatus.BLOCKED,
        Transition.REQUEST_INPUT: TaskStatus.WAITING_FOR_INPUT,
        Transition.REQUEST_APPROVAL: TaskStatus.WAITING_FOR_APPROVAL,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
        Transition.FAIL: TaskStatus.FAILED,
    },
    TaskStatus.RUNNING: {
        Transition.COMPLETE: TaskStatus.COMPLETED,
        Transition.BLOCK: TaskStatus.BLOCKED,
        Transition.REQUEST_INPUT: TaskStatus.WAITING_FOR_INPUT,
        Transition.REQUEST_APPROVAL: TaskStatus.WAITING_FOR_APPROVAL,
        Transition.FAIL: TaskStatus.FAILED,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
    },
    TaskStatus.BLOCKED: {
        Transition.UNBLOCK: TaskStatus.RUNNING,
        Transition.ASSIGN: TaskStatus.ASSIGNED,
        Transition.FAIL: TaskStatus.FAILED,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
    },
    TaskStatus.WAITING_FOR_INPUT: {
        Transition.PROVIDE_INPUT: TaskStatus.RUNNING,
        Transition.FAIL: TaskStatus.FAILED,
        Transition.CANCEL: TaskStatus.CANCELED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
    },
    TaskStatus.WAITING_FOR_APPROVAL: {
        Transition.APPROVAL_GRANTED: TaskStatus.RUNNING,
        Transition.APPROVAL_REJECTED: TaskStatus.FAILED,
        Transition.EXPIRE: TaskStatus.EXPIRED,
        Transition.CANCEL: TaskStatus.CANCELED,
    },
    TaskStatus.COMPLETED: {},
    TaskStatus.FAILED: {},
    TaskStatus.CANCELED: {},
    TaskStatus.EXPIRED: {},
}

#: States in which no progress happens unless an external event arrives. A task
#: stuck in one of these needs a timer; see `docs/OPERATIONS.md` stuck-task sweeps.
PASSIVE_TASK_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_FOR_INPUT,
        TaskStatus.WAITING_FOR_APPROVAL,
    }
)


# ----------------------------------------------------------- agent lifecycle --
AGENT_TRANSITIONS: Final[
    Mapping[AgentLifecycleStatus, Mapping[Transition, AgentLifecycleStatus]]
] = {
    AgentLifecycleStatus.DRAFT: {
        Transition.ACTIVATE: AgentLifecycleStatus.PROVISIONING,
        Transition.SUSPEND: AgentLifecycleStatus.SUSPENDED,
    },
    AgentLifecycleStatus.PROVISIONING: {
        Transition.ACTIVATE: AgentLifecycleStatus.ACTIVE,
        Transition.FAIL: AgentLifecycleStatus.DRAFT,
        Transition.SUSPEND: AgentLifecycleStatus.SUSPENDED,
    },
    AgentLifecycleStatus.ACTIVE: {
        Transition.PAUSE: AgentLifecycleStatus.PAUSED,
        Transition.DEGRADE: AgentLifecycleStatus.DEGRADED,
        Transition.SUSPEND: AgentLifecycleStatus.SUSPENDED,
        Transition.RETIRE: AgentLifecycleStatus.RETIRED,
    },
    AgentLifecycleStatus.PAUSED: {
        Transition.RESUME: AgentLifecycleStatus.ACTIVE,
        Transition.SUSPEND: AgentLifecycleStatus.SUSPENDED,
        Transition.RETIRE: AgentLifecycleStatus.RETIRED,
    },
    AgentLifecycleStatus.DEGRADED: {
        Transition.RESUME: AgentLifecycleStatus.ACTIVE,
        Transition.PAUSE: AgentLifecycleStatus.PAUSED,
        Transition.SUSPEND: AgentLifecycleStatus.SUSPENDED,
        Transition.RETIRE: AgentLifecycleStatus.RETIRED,
    },
    AgentLifecycleStatus.SUSPENDED: {
        Transition.RESUME: AgentLifecycleStatus.PAUSED,
        Transition.RETIRE: AgentLifecycleStatus.RETIRED,
    },
    AgentLifecycleStatus.RETIRED: {},
}


# ------------------------------------------------------------ approval model --
# Written out rather than derived: approval transitions are keyed by a human
# decision, not by a lifecycle event, so there is no uniform event to map.
_APPROVAL_GRAPH: Final[Mapping[ApprovalStatus, frozenset[ApprovalStatus]]] = {
    ApprovalStatus.PENDING: frozenset(
        {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.NEEDS_INFORMATION}
    ),
    ApprovalStatus.NEEDS_INFORMATION: frozenset(
        {
            ApprovalStatus.PENDING,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELED,
            ApprovalStatus.EXPIRED,
        }
    ),
    ApprovalStatus.APPROVED: frozenset({ApprovalStatus.CANCELED}),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.CANCELED: frozenset(),
}


def _require(
    machine: Mapping[str, Mapping[Transition, str]], current: str, event: Transition, label: str
) -> str:
    allowed = machine.get(current, {})
    target = allowed.get(event)
    if target is None:
        legal = ", ".join(sorted(machine.get(current, {}).keys())) or "none"
        msg = (
            f"illegal {label} transition: {current} --{event.value}--> ? "
            f"(legal from {current}: {legal})"
        )
        raise PreconditionError(
            msg, details={"from": current, "event": event.value, "allowed": legal}
        )
    return target


def next_task_status(current: TaskStatus, event: Transition) -> TaskStatus:
    """Apply an event to the task machine. Raises on an illegal jump."""
    return TaskStatus(_require(TASK_TRANSITIONS, current, event, "task"))  # type: ignore[arg-type]


def can_transition_task(current: TaskStatus, event: Transition) -> bool:
    return event in TASK_TRANSITIONS.get(current, {})


def allowed_task_transitions(current: TaskStatus) -> frozenset[Transition]:
    return frozenset(TASK_TRANSITIONS.get(current, {}))


def is_terminal_task(status: TaskStatus) -> bool:
    return not TASK_TRANSITIONS.get(status)


def next_agent_status(current: AgentLifecycleStatus, event: Transition) -> AgentLifecycleStatus:
    return AgentLifecycleStatus(_require(AGENT_TRANSITIONS, current, event, "agent"))  # type: ignore[arg-type]


def can_transition_agent(current: AgentLifecycleStatus, event: Transition) -> bool:
    return event in AGENT_TRANSITIONS.get(current, {})


def next_approval_status(current: ApprovalStatus, target: ApprovalStatus) -> ApprovalStatus:
    if target not in _APPROVAL_GRAPH.get(current, frozenset()):
        msg = f"illegal approval transition: {current} -> {target}"
        raise PreconditionError(msg, details={"from": current, "to": target})
    return target


def can_transition_approval(current: ApprovalStatus, target: ApprovalStatus) -> bool:
    return target in _APPROVAL_GRAPH.get(current, frozenset())


# ------------------------------------------------------ delegation / subagent --
DELEGATION_TRANSITIONS: Final[Mapping[DelegationStatus, Mapping[Transition, DelegationStatus]]] = {
    DelegationStatus.PROPOSED: {
        Transition.ACCEPT: DelegationStatus.ACCEPTED,
        Transition.REJECT: DelegationStatus.REJECTED,
        Transition.CANCEL: DelegationStatus.CANCELED,
        Transition.EXPIRE: DelegationStatus.TIMED_OUT,
    },
    DelegationStatus.ACCEPTED: {
        Transition.BEGIN_WORK: DelegationStatus.IN_PROGRESS,
        Transition.REJECT: DelegationStatus.REJECTED,
        Transition.CANCEL: DelegationStatus.CANCELED,
        Transition.EXPIRE: DelegationStatus.TIMED_OUT,
    },
    DelegationStatus.IN_PROGRESS: {
        Transition.COMPLETE: DelegationStatus.COMPLETED,
        Transition.FAIL: DelegationStatus.FAILED,
        Transition.BLOCK: DelegationStatus.BLOCKED,
        Transition.CANCEL: DelegationStatus.CANCELED,
        Transition.EXPIRE: DelegationStatus.TIMED_OUT,
    },
    DelegationStatus.BLOCKED: {
        Transition.UNBLOCK: DelegationStatus.IN_PROGRESS,
        Transition.FAIL: DelegationStatus.FAILED,
        Transition.CANCEL: DelegationStatus.CANCELED,
    },
    DelegationStatus.COMPLETED: {},
    DelegationStatus.FAILED: {},
    DelegationStatus.REJECTED: {},
    DelegationStatus.CANCELED: {},
    DelegationStatus.TIMED_OUT: {},
}


def next_delegation_status(current: DelegationStatus, event: Transition) -> DelegationStatus:
    return DelegationStatus(
        _require(DELEGATION_TRANSITIONS, current, event, "delegation")  # type: ignore[arg-type]
    )


SUBAGENT_TRANSITIONS: Final[Mapping[SubagentStatus, Mapping[Transition, SubagentStatus]]] = {
    SubagentStatus.SPAWNING: {
        Transition.BEGIN_WORK: SubagentStatus.RUNNING,
        Transition.FAIL: SubagentStatus.FAILED,
        Transition.CANCEL: SubagentStatus.CANCELED,
        Transition.EXPIRE: SubagentStatus.EXPIRED,
    },
    SubagentStatus.RUNNING: {
        Transition.COMPLETE: SubagentStatus.COMPLETED,
        Transition.FAIL: SubagentStatus.FAILED,
        Transition.CANCEL: SubagentStatus.CANCELED,
        Transition.EXPIRE: SubagentStatus.EXPIRED,
    },
    SubagentStatus.COMPLETED: {},
    SubagentStatus.FAILED: {},
    SubagentStatus.CANCELED: {},
    SubagentStatus.EXPIRED: {},
}


def next_subagent_status(current: SubagentStatus, event: Transition) -> SubagentStatus:
    return SubagentStatus(
        _require(SUBAGENT_TRANSITIONS, current, event, "subagent")  # type: ignore[arg-type]
    )


__all__ = [
    "AGENT_TRANSITIONS",
    "DELEGATION_TRANSITIONS",
    "PASSIVE_TASK_STATUSES",
    "SUBAGENT_TRANSITIONS",
    "TASK_TRANSITIONS",
    "Transition",
    "allowed_task_transitions",
    "can_transition_agent",
    "can_transition_approval",
    "can_transition_task",
    "is_terminal_task",
    "next_agent_status",
    "next_approval_status",
    "next_delegation_status",
    "next_subagent_status",
    "next_task_status",
]
