"""Core execution contracts.

These types are the stability boundary of the whole platform. Swapping
PydanticAI for LangGraph, or any other runtime, must not change a single line
here — `tests/unit/test_runtime_swap.py` asserts exactly that.

If a framework-specific type ever appears in this module, the boundary has
leaked and the swap guarantee is gone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.delegation import DelegationLimits, DelegationPath
from ai_orchestrator.domain.enums import (
    ActorType,
    AutonomyLevel,
    DataClassification,
    DelegationStatus,
    EffectClass,
    RiskLevel,
    RunMode,
    TaskType,
    ToolRisk,
)
from ai_orchestrator.domain.ids import (
    ApprovalId,
    ExecutionId,
    OrganizationId,
    OrgUnitId,
    RoleId,
    SkillId,
    SubagentRunId,
    TaskId,
    ToolId,
)


class Actor(BaseModel):
    """Who is acting.

    Identity is never taken from an LLM. It is attached by the control plane
    from a verified credential, then travels with the request. The `kind` field
    exists so an agent cannot be mistaken for a human approver downstream.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ActorType
    display_name: str = ""
    organization_id: OrganizationId | None = None
    org_unit_id: OrgUnitId | None = None
    role_id: RoleId | None = None
    # Only meaningful for ActorType.HUMAN. Present so approval checks can
    # verify maker-checker separation.
    is_privileged_human: bool = False

    @property
    def is_human(self) -> bool:
        return self.kind is ActorType.HUMAN

    @property
    def is_agent(self) -> bool:
        return self.kind is ActorType.AGENT


class AgentTask(BaseModel):
    """The unit of work handed to a runtime.

    A runtime receives this and nothing else. It may not look up its own
    authority, discover its own tools, or read the organisation graph: the
    context it is allowed to see is already assembled and already authorised.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: TaskId
    organization_id: OrganizationId
    goal: str
    task_type: TaskType = TaskType.EXECUTION
    execution_id: ExecutionId
    deadline: datetime | None = None
    # Free-form, schema-validated by the caller. Never trusted as instructions.
    input: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output_schema: dict[str, Any] | None = None
    parent_task_id: TaskId | None = None
    is_subagent: bool = False


class DelegateOption(BaseModel):
    """One colleague this agent may hand work to, and what choosing them costs.

    Carries the three things a manager actually decides on: what the person does,
    what they are good at, and how much they are already carrying. `active_tasks`
    is read from the registry, not estimated, so a busy colleague is visibly busy.
    """

    model_config = ConfigDict(frozen=True)

    agent_name: str
    purpose: str
    capabilities: tuple[str, ...] = ()
    active_tasks: int = 0
    #: What the person is doing right now, in their own words. Empty when they
    #: are idle — which is itself the signal that they are available.
    current_work: tuple[str, ...] = ()


class ToolContract(BaseModel):
    """A capability the agent may propose using.

    The agent does not receive the tool itself. It receives a description of
    what it is allowed to ask for; the platform decides whether to run it.
    """

    model_config = ConfigDict(frozen=True)

    tool_id: ToolId
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    risk: ToolRisk = ToolRisk.LOW_RISK_WRITE
    effect_class: EffectClass = EffectClass.MUTATE_INTERNAL
    requires_approval: bool = False
    timeout_s: int = 30
    data_classification: DataClassification = DataClassification.INTERNAL
    # Tools the agent may reach through MCP rather than in-process.
    mcp_server: str | None = None


class SkillContract(BaseModel):
    """A reusable capability layer bound to the agent."""

    model_config = ConfigDict(frozen=True)

    skill_id: SkillId
    name: str
    version: str
    description: str
    instructions: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    required_tool_ids: tuple[ToolId, ...] = ()
    risk_level: RiskLevel = RiskLevel.LOW


class MemoryScope(BaseModel):
    """Ceiling on what the agent may retrieve.

    Enforced by the memory service, not by the model. Passed into the context
    builder so the runtime never has to guess its own visibility.
    """

    model_config = ConfigDict(frozen=True)

    organization_id: OrganizationId
    org_unit_ids: tuple[OrgUnitId, ...] = ()
    agent_ids: tuple[str, ...] = ()
    task_ids: tuple[TaskId, ...] = ()
    max_classification: DataClassification = DataClassification.INTERNAL
    max_results: int = 8
    max_tokens: int = 8_000


class BudgetEnvelope(BaseModel):
    """Hard spend ceiling for one execution."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    max_cost_usd: Money
    max_runtime_s: int
    #: Ceiling on model calls for one execution. Separate from `max_tokens`
    #: because tokens bound a *call* and this bounds the *loop*: a model that
    #: keeps returning tool calls can stay under every token and cost limit while
    #: spending the organisation's money indefinitely, which is what the real
    #: model did — 23 turns on one goal, each of them legal.
    #:
    #: **It was 24, chosen from one observation. It is now 48, chosen from a distribution.**
    #: Measured over every run on this tenant that has recorded model calls
    #: (`count(model_usage) per execution`), after removing the demo residue:
    #:
    #: | outcome | n | min | p50 | p90 | max |
    #: |---|---|---|---|---|---|
    #: | succeeded | 18 | 1 | **1** | 23 | **24** |
    #: | failed (`budget_exhausted`) | 3 | 24 | 24 | 24 | 24 |
    #:
    #: Three things follow, and the third is why the number moved:
    #:
    #: 1. **The median successful run makes one model call.** The ceiling is 48x the typical
    #:    run, so it costs nothing for ordinary work and only ever bites the tail. That is the
    #:    shape worth having in a limit.
    #: 2. **All three failures stopped at exactly 24.** They were stopped, not self-terminated.
    #:    A run that ends at the ceiling with `budget_exhausted` is a run that wanted to keep
    #:    going, and for a confused model that means looping.
    #: 3. **One successful run used exactly 24 and finished anyway.** So 24 was not slack --
    #:    it was the boundary. There is no evidence any run would have *succeeded* at 25, and
    #:    the three failures give no reason to think 48 rescues them; they would run longer and
    #:    fail at higher cost. So this doubles the headroom for the hardest work that is
    #:    *known* to finish, and keeps a real ceiling.
    #:
    #: **What would make this better, and has not been done:** a per-task request ceiling.
    #: `BudgetState` is built in one place, so a task that genuinely needs 200 calls could say
    #: so the way it already says so with `budget_limit_tokens`. The evidence for what to
    #: default to is thin -- one run at the boundary -- so a per-task override would let the
    #: data set the number instead of a constant guessing it.
    #:
    #: **Removing this limit is not the fix.** The comment above explains what it is for, and
    #: the three failures are exactly the case it exists for: unbounded spending on a model
    #: that has lost the thread.
    max_requests: int = 48
    #: Ceiling on tool calls, which is the other way a loop can run away: one
    #: model call, twenty tool calls, and the budget is untouched.
    #:
    #: The number is a placeholder and the comment above says so: tool calls are
    #: not yet counted per execution (F206), so there is no distribution behind
    #: any value here. What is measured is that successful runs make a median of
    #: one call, and that every failure stopped at the ceiling. Raise it with
    #: that in mind — a higher ceiling buys a confused model more turns, it does
    #: not make it more capable.
    max_tool_calls: int = 48
    spent_tokens: int = 0
    spent_cost_usd: Money = Money("0")

    @field_validator("max_cost_usd", "spent_cost_usd", mode="before")
    @classmethod
    def _coerce_money(cls, value: object) -> object:
        return Money(str(value)) if value is not None else value

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.spent_tokens)

    def remaining_cost_usd(self) -> Money:
        return max(Money("0"), self.max_cost_usd - self.spent_cost_usd)


class AgentContext(BaseModel):
    """Everything a runtime is allowed to know.

    Assembled by the platform from system instructions, role, task, authorised
    memory, authorised tools and policy constraints. There is no path by which a
    runtime adds to this itself — that is the whole point of having a context
    builder rather than letting the agent assemble its own prompt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: Actor
    task: AgentTask
    system_instructions: str
    role_name: str = ""
    #: Who this agent may delegate to, as `(name, what they do)`.
    #:
    #: Supplied by the control plane, never guessed. Asked to pick a colleague
    #: without a list, a model invents a plausible department — `Procurement`,
    #: when this company has no such department — and the delegation is then
    #: refused by the platform. The refusal is correct and useless: the goal is
    #: lost because the model could not spell a name it was never shown.
    delegate_targets: tuple[tuple[str, str], ...] = ()
    #: The same people as `delegate_targets`, with what they can actually do and
    #: what they are already carrying. A name and a unit purpose is enough to
    #: satisfy the schema and not enough to choose: a manager reading six
    #: identical lines cannot tell a free agent from a loaded one, so it spreads
    #: work evenly across colleagues who are already at capacity. This is the
    #: part that makes "who is this for" answerable from the data rather than
    #: from the model's impression.
    delegate_options: tuple[DelegateOption, ...] = ()
    #: From settings, carried in so one number governs every run.
    #:
    #: This default and `settings.max_tool_calls` were 24 in one place and 48 in
    #: the other during this session, which is the worst shape a number can
    #: have: correct in whichever code path a test happened to exercise. Both now
    #: read 48 and both say where the real value comes from, so a change is one
    #: edit in `settings.py` and a mismatch is a visible diff rather than a
    #: ceiling that depends on who called.
    max_tool_calls: int = 48
    organization_id: OrganizationId
    org_unit_id: OrgUnitId | None = None
    authorized_tools: tuple[ToolContract, ...] = ()
    authorized_skills: tuple[SkillContract, ...] = ()
    memory: MemoryScope | None = None
    delegation_path: DelegationPath = Field(default_factory=DelegationPath)
    delegation_limits: DelegationLimits = Field(default_factory=DelegationLimits.platform_default)
    budget: BudgetEnvelope
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_LOW_RISK_AUTONOMOUS
    run_mode: RunMode = RunMode.LIVE
    model_profile: str = "default"
    deadline: datetime | None = None
    # Retrieved memory and prior work, already scoped and already formatted.
    retrieved_context: tuple[str, ...] = ()
    # Earlier turns of this task, oldest first. Kept separate from
    # `retrieved_context` because the two have different trust properties: a
    # retrieved passage is untrusted third-party text that must be delimited and
    # cited, while a prior turn is this agent's own chain of work and is
    # ordinary context. Merging them would force the stricter handling onto
    # material that does not need it, or — worse — relax it for the material
    # that does.
    history: tuple[str, ...] = ()
    data_classification: DataClassification = DataClassification.INTERNAL
    # Version stamps for reproducibility. Required to answer
    # "why did this run behave this way?" months later.
    agent_definition_version: int = 1
    policy_version: int = 1
    workflow_version: str = "v1"

    def tool_by_name(self, name: str) -> ToolContract | None:
        return next((t for t in self.authorized_tools if t.name == name), None)

    def skill_by_name(self, name: str) -> SkillContract | None:
        return next((s for s in self.authorized_skills if s.name == name), None)


class ActionProposal(BaseModel):
    """A typed thing the agent wants to do.

    The agent proposes; policy decides. An LLM never performs a side effect
    directly — it emits one of these, and the platform decides whether to
    execute it, require approval, or deny it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[
        "tool_call", "delegate", "spawn_subagent", "request_input", "escalate", "complete"
    ]
    tool_id: ToolId | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    target_agent_id: str | None = None
    objective: str = ""
    rationale: str = ""
    # The agent's own read of the risk. Advisory only — the platform recomputes
    # risk from the tool registry and never trusts this value.
    self_assessed_risk: RiskLevel = RiskLevel.LOW

    @model_validator(mode="after")
    def _validate_shape(self) -> ActionProposal:
        """Check that a proposal names what it proposes to act on.

        A `model_validator` rather than a `field_validator`, because the check
        spans fields: a field validator only sees the fields declared *before*
        it, so `kind` could never see `tool_id`. That is a silent hole — the
        validator appeared to work and accepted tool calls that named nothing.
        """
        if self.kind == "tool_call" and not (self.tool_id or self.tool_name):
            msg = "a tool_call proposal must name a tool"
            raise ValueError(msg)
        if self.kind in {"delegate", "spawn_subagent"} and not self.target_agent_id:
            msg = f"a {self.kind} proposal must name a target agent"
            raise ValueError(msg)
        if self.kind == "complete" and not self.objective and not self.rationale:
            msg = "a complete proposal must state an outcome or a rationale"
            raise ValueError(msg)
        return self


class ArtifactRef(BaseModel):
    """A pointer to something produced. Never inlined content."""

    model_config = ConfigDict(frozen=True)

    kind: str
    uri: str
    content_type: str = "application/json"
    bytes: int | None = None
    sha256: str | None = None
    classification: DataClassification = DataClassification.INTERNAL
    # Where it was written, so a reviewer can find it without parsing the chat log.
    memory_item_id: str | None = None
    document_id: str | None = None


class Escalation(BaseModel):
    """A structured "I am stopping, a human must decide"."""

    model_config = ConfigDict(frozen=True)

    reason: str
    detail: str = ""
    severity: RiskLevel = RiskLevel.MEDIUM
    approval_id: ApprovalId | None = None
    # What the agent was trying to do, so the human can decide without guessing.
    blocked_action: str = ""


class AgentResultStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_INPUT = "needs_input"
    DELEGATED = "delegated"
    CANCELED = "canceled"


class AgentResult(BaseModel):
    """The typed outcome of one execution.

    Not a string. A plain-text summary is carried inside this object, never
    instead of it, because the summary is for humans and this is for the system.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AgentResultStatus
    summary: str
    execution_id: ExecutionId
    task_id: TaskId
    artifacts: tuple[ArtifactRef, ...] = ()
    follow_up_actions: tuple[ActionProposal, ...] = ()
    escalations: tuple[Escalation, ...] = ()
    # Populated by the platform after the call, not by the agent.
    usage: TokenUsage | None = None
    cost_usd: Money | None = None
    model_used: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    # Sub-agent runs created during this execution, for the parent to await.
    subagent_run_ids: tuple[SubagentRunId, ...] = ()
    output: dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is AgentResultStatus.COMPLETED

    @property
    def is_terminal_failure(self) -> bool:
        return self.status in {AgentResultStatus.FAILED, AgentResultStatus.CANCELED}


@runtime_checkable
class AgentRuntime(Protocol):
    """The framework seam.

    PydanticAI is the primary implementation. Any other framework is an
    adapter behind this signature, and none of them may own organization
    state, the task model, authorization, budget, policy or memory.
    """

    name: str

    async def execute(
        self,
        task: AgentTask,
        context: AgentContext,
        *,
        record_usage: Callable[[Any], Awaitable[None]] | None = None,
        execute_tool: Callable[..., Awaitable[Any]] | None = None,
    ) -> AgentResult:
        """Run one task. Must honour the context's budget, deadline and tool set.

        `record_usage` is called once per model call with whatever the runtime's
        model layer returned, routing decision included. A runtime that makes no
        model calls may ignore it. It is optional because a runtime that owns no
        model layer has nothing to report — and because a caller that does not
        want a per-call ledger should not have to construct one.

        Contract requirements for every implementation:
          * never perform an external side effect directly;
          * never retrieve memory outside `context.memory`;
          * never exceed `context.budget`;
          * return a typed `AgentResult`, never a bare string;
          * never raise a non-`PlatformError` for an expected failure.
        """
        ...


class RuntimeCapabilities(BaseModel):
    """What a runtime supports. Lets the platform pick sensibly."""

    model_config = ConfigDict(frozen=True)

    name: str
    streaming: bool = False
    tool_calling: bool = True
    structured_output: bool = True
    subagents: bool = False
    checkpoints: bool = False
    max_concurrency: int = 8
    # Cost of adopting this runtime, for docs/BENCHMARKS.md.
    maturity: Literal["experimental", "adapter", "foundation"] = "adapter"


class DelegationRequest(BaseModel):
    """Structured delegation, never a chat message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delegation_id: str
    parent_task_id: TaskId
    source_agent_id: str
    target_agent_id: str
    objective: str
    input: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    deadline: datetime | None = None
    budget: BudgetEnvelope | None = None
    required_output_schema: dict[str, Any] | None = None
    approval_required: bool = False
    # Stamped by the caller, for the same reason as `ExecutionMetadata.started_at`.
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExecutionMetadata(BaseModel):
    """Reproducibility record for one execution.

    Everything needed to explain and re-run the execution, and deliberately not
    the private reasoning: the platform stores decisions and inputs, not chain
    of thought.
    """

    model_config = ConfigDict(frozen=True)

    execution_id: ExecutionId
    task_id: TaskId
    organization_id: OrganizationId
    agent_id: str
    agent_definition_version: int
    skill_versions: dict[str, str] = Field(default_factory=dict)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    model_profile: str
    model_used: str | None = None
    policy_version: int = 1
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    trace_id: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    # Timestamps are required, never defaulted to `now()`. A value object whose
    # fields depend on when it was built cannot be tested for TTL, expiry or
    # staleness without freezing the clock, and those are exactly the behaviours
    # the platform has to get right. The application layer stamps them.
    started_at: datetime
    finished_at: datetime | None = None
    delegation_status: DelegationStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


NonEmptyStr = Annotated[str, Field(min_length=1)]


__all__ = [
    "ActionProposal",
    "Actor",
    "AgentContext",
    "AgentResult",
    "AgentResultStatus",
    "AgentRuntime",
    "AgentTask",
    "ArtifactRef",
    "BudgetEnvelope",
    "DelegationRequest",
    "Escalation",
    "ExecutionMetadata",
    "MemoryScope",
    "NonEmptyStr",
    "RuntimeCapabilities",
    "SkillContract",
    "ToolContract",
]
