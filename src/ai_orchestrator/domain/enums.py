"""Domain enumerations.

These strings are part of the contract: they appear in the API, in NATS
subjects, in the database, and in audit rows. Renaming a member is a breaking
change and requires a migration.
"""

from __future__ import annotations

from enum import StrEnum


class AgentLifecycleStatus(StrEnum):
    """Business lifecycle of an agent instance.

    Deliberately never contains `deleted`: audit history must survive the
    entity. Retirement is the terminal state.
    """

    DRAFT = "draft"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class AgentRuntimeStatus(StrEnum):
    """Operational state, orthogonal to lifecycle.

    An agent can be ACTIVE (a business fact) while its runtime is BUSY (an
    operational fact). Conflating them produces contradictions like
    "Agent = ready, Task = blocked" that are neither true nor false.
    """

    IDLE = "idle"
    READY = "ready"
    BUSY = "busy"
    WAITING = "waiting"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class AgentHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    """Canonical task state machine states."""

    CREATED = "created"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


#: Terminal states: no transition leaves these.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.EXPIRED}
)


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class DelegationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"


class SubagentStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_INFORMATION = "needs_information"
    EXPIRED = "expired"
    CANCELED = "canceled"


class RiskLevel(StrEnum):
    """Ordinal. Comparison uses `_RISK_ORDER`, not string order."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    PRIVILEGED = "privileged"


_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
    RiskLevel.PRIVILEGED: 4,
}


def risk_at_least(risk: RiskLevel, floor: RiskLevel) -> bool:
    return _RISK_ORDER[risk] >= _RISK_ORDER[floor]


def max_risk(*risks: RiskLevel) -> RiskLevel:
    return max(risks, key=lambda r: _RISK_ORDER[r])


class PolicyDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    ESCALATE = "escalate"


class AutonomyLevel(StrEnum):
    """How much the agent may do without a human.

    Configuration, not a property of the agent alone: the same agent under a
    stricter policy is a different effective agent.
    """

    L0_SUGGEST = "l0_suggest"
    L1_LOW_RISK_AUTONOMOUS = "l1_low_risk_autonomous"
    L2_PARENT_REVIEW = "l2_parent_review"
    L3_HUMAN_APPROVAL = "l3_human_approval"
    L4_BOUNDED_AUTONOMOUS = "l4_bounded_autonomous"

    @classmethod
    def _missing_(cls, value: object) -> AutonomyLevel | None:
        """Accept the **short** form as well: `L1` for `l1_low_risk_autonomous`.

        Two vocabularies for one concept, and they disagreed for the whole life of the
        project. `persistence/process.py` names the dossier's levels `L1` through `L4`
        -- the form a person reads and the form migration `0016` writes into
        `agents.granted_level` and `autonomy_ceiling` -- while this enum uses the long
        machine form. So `AutonomyLevel("L1")` raised `ValueError`, and **every agent row in
        the database carried a value the domain could not parse.**

        It survived 2,600 tests because nothing that ran in the suite ever parsed the
        column: the only code that does is the executor's grant check, and the one path
        that reached it in practice used an agent whose level was never read from the row.
        It was found by `scripts/run_fleet.py`, on the first run, seven agents out of eight:
        `ValueError: 'L1' is not a valid AutonomyLevel`.

        The fix is at the **boundary**, not at every call site: parsing accepts both, and
        `str()` still yields the long form, so nothing that renders or persists the enum
        changes. The alternative -- rewriting the database to the long form -- would be a
        migration on data that a person reads as `L1` in the UI and in the dossier, for a
        purely internal disagreement.
        """
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        # `L3` and `l3` both mean `l3_human_approval`; `L3_HUMAN_APPROVAL` is already a
        # member and never reaches here.
        short = text.upper()
        if len(short) == 2 and short[0] == "L" and short[1].isdigit():
            try:
                return cls(f"l{short[1]}_{_LONG_NAMES[short[1]]}")
            except KeyError, ValueError:
                return None
        return None


#: The tail of each long name, so `_missing_` can rebuild it from `L<n>`. Derived from the
#: enum itself would be circular; a literal is honest and greppable, and
#: `test_the_short_and_long_forms_agree` fails the day the two drift apart.
_LONG_NAMES = {
    "0": "suggest",
    "1": "low_risk_autonomous",
    "2": "parent_review",
    "3": "human_approval",
    "4": "bounded_autonomous",
}


class AuthorityScope(StrEnum):
    ORGANIZATION = "organization"
    DEPARTMENT = "department"
    TASK = "task"
    DATA = "data"
    TOOL = "tool"
    MODEL = "model"
    BUDGET = "budget"
    DELEGATION = "delegation"
    DEPLOYMENT = "deployment"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    # Not a credential: the classification of data that must not leave a
    # trusted boundary. The name is what the rule is matching on.
    SECRET = "secret"  # noqa: S105


#: Higher classification requires a stricter provider allowlist.
_CLASSIFICATION_ORDER: dict[DataClassification, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
    DataClassification.SECRET: 4,
}


def classification_at_least(value: DataClassification, floor: DataClassification) -> bool:
    return _CLASSIFICATION_ORDER[value] >= _CLASSIFICATION_ORDER[floor]


def classification_exceeds(value: DataClassification, ceiling: DataClassification) -> bool:
    """True when `value` is *above* the ceiling.

    Distinct from `classification_at_least`, which is inclusive. Using the
    inclusive form for a ceiling check rejects the exact match, so a provider
    approved for `restricted` data could not receive `restricted` data — the kind
    of off-by-one that silently sends everything down the fallback path.
    """
    return _CLASSIFICATION_ORDER[value] > _CLASSIFICATION_ORDER[ceiling]


class EffectClass(StrEnum):
    """What an action does to the world.

    Adapted from the effect-class taxonomy that survived a real production E2E
    in the O-Nexus HR agent (`config/action-registry.json`). It is a better
    basis for approval policy than a flat risk score because it describes the
    *kind* of consequence, which is what a policy can reason about.
    """

    READ = "read"
    PREPARE = "prepare"
    MUTATE_INTERNAL = "mutate_internal"
    APPROVE = "approve"
    DECIDE = "decide"
    EXTERNAL_SEND = "external_send"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    LOW_RISK_WRITE = "low_risk_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    PRIVILEGED = "privileged"
    DESTRUCTIVE = "destructive"


#: Tool risk is a separate ordering from `RiskLevel`. They are related but not
#: identical, and reusing one enum's table for the other raises KeyError on the
#: first comparison.
_TOOL_RISK_ORDER: dict[ToolRisk, int] = {
    ToolRisk.READ_ONLY: 0,
    ToolRisk.LOW_RISK_WRITE: 1,
    ToolRisk.EXTERNAL_SIDE_EFFECT: 2,
    ToolRisk.PRIVILEGED: 3,
    ToolRisk.DESTRUCTIVE: 4,
}


def tool_risk_at_least(risk: ToolRisk, ceiling: ToolRisk) -> bool:
    """True when `risk` is at or above `ceiling`."""
    return _TOOL_RISK_ORDER[risk] >= _TOOL_RISK_ORDER[ceiling]


def tool_risk_exceeds(risk: ToolRisk, ceiling: ToolRisk) -> bool:
    """True when `risk` is strictly above `ceiling`."""
    return _TOOL_RISK_ORDER[risk] > _TOOL_RISK_ORDER[ceiling]


class SkillGovernanceState(StrEnum):
    TRUSTED = "trusted"
    REVIEWED = "reviewed"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"
    BLOCKED = "blocked"


class MemoryTier(StrEnum):
    """Ordered from shortest to longest lived. See docs/MEMORY.md."""

    RUN_CONTEXT = "run_context"
    WORKING = "working"
    TASK_EPISODIC = "task_episodic"
    AGENT = "agent"
    DEPARTMENT = "department"
    ORGANIZATION = "organization"
    SEMANTIC = "semantic"
    AUDIT = "audit"


class ActorType(StrEnum):
    """Identity classes are kept separate on purpose (§134).

    A human signature, a service token, an agent identity and a workflow
    identity are different credentials with different blast radii.
    """

    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"
    WORKFLOW = "workflow"
    SYSTEM = "system"


class TaskType(StrEnum):
    """Generic work kinds. Not business domains — a department is not a task type."""

    RESEARCH = "research"
    ANALYSIS = "analysis"
    REPORT = "report"
    REVIEW = "review"
    DECISION = "decision"
    EXECUTION = "execution"
    COORDINATION = "coordination"


class RunMode(StrEnum):
    """Simulation mocks every external side effect. Used by tests and demos."""

    LIVE = "live"
    SIMULATION = "simulation"
    REPLAY = "replay"


class EventType(StrEnum):
    """Event vocabulary. The string is the stable wire contract.

    Naming: `<aggregate>.<past-tense-verb>`. Version travels in the envelope.
    """

    ORGANIZATION_CREATED = "organization.created"
    ORG_UNIT_CREATED = "org_unit.created"
    AGENT_CREATED = "agent.created"
    AGENT_STATUS_CHANGED = "agent.status_changed"
    AGENT_RETIRED = "agent.retired"
    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_STARTED = "task.started"
    TASK_UPDATED = "task.updated"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELED = "task.canceled"
    DELEGATION_CREATED = "delegation.created"
    DELEGATION_ACCEPTED = "delegation.accepted"
    DELEGATION_REJECTED = "delegation.rejected"
    DELEGATION_COMPLETED = "delegation.completed"
    DELEGATION_BLOCKED = "delegation.blocked"
    SUBAGENT_SPAWNED = "subagent.spawned"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_EXPIRED = "approval.expired"
    TOOL_INVOKED = "tool.invoked"
    TOOL_FAILED = "tool.failed"
    A2A_CALL_SENT = "a2a.call_sent"
    A2A_CALL_COMPLETED = "a2a.call_completed"
    A2A_CALL_FAILED = "a2a.call_failed"
    MODEL_CALL_COMPLETED = "model.call_completed"
    MODEL_CALL_FAILED = "model.call_failed"
    SKILL_PROPOSED = "skill.proposed"
    SKILL_PUBLISHED = "skill.published"
    POLICY_DENIED = "policy.denied"
    MEMORY_WRITTEN = "memory.written"
    AGENT_DISCOVERED = "agent.discovered"


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "ActorType",
    "AgentHealth",
    "AgentLifecycleStatus",
    "AgentRuntimeStatus",
    "ApprovalStatus",
    "AuthorityScope",
    "AutonomyLevel",
    "DataClassification",
    "DelegationStatus",
    "EffectClass",
    "EventType",
    "MemoryTier",
    "PolicyDecisionType",
    "RiskLevel",
    "RunMode",
    "SkillGovernanceState",
    "SubagentStatus",
    "TaskPriority",
    "TaskStatus",
    "TaskType",
    "ToolRisk",
    "classification_at_least",
    "max_risk",
    "risk_at_least",
    "tool_risk_at_least",
    "tool_risk_exceeds",
]
