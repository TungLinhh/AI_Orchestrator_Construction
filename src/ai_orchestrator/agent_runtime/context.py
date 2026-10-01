"""Context assembly.

The single place that decides what an agent is allowed to see. Two rules:

  * nothing is added by the runtime. The runtime receives what this module
    produced and has no way to widen it, which is what makes "the agent could not
    see X" a structural property rather than a promise.
  * the context budget is enforced here, not by the model. When instructions,
    task, memory, tool schemas and history together exceed the window, the
    overflow is compacted in a defined order (see `_compact`) rather than left
    to the provider to truncate, because a provider that silently drops the
    system prompt produces an agent that behaves correctly for the wrong reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.contracts import (
    Actor,
    AgentContext,
    AgentTask,
    BudgetEnvelope,
    DelegateOption,
    MemoryScope,
    SkillContract,
    ToolContract,
)
from ai_orchestrator.domain.delegation import DelegationLimits, DelegationPath
from ai_orchestrator.domain.enums import (
    ActorType,
    AutonomyLevel,
    DataClassification,
    RunMode,
)
from ai_orchestrator.domain.errors import ValidationError

#: Rough characters-per-token ratio for English and Vietnamese prose. Used only
#: for budgeting the context window, never for billing.
CHARS_PER_TOKEN = 4

#: Sections are compacted in this order, least useful first. Identity and
#: instructions are never compacted: an agent that forgets who it is or what it
#: is for will produce confident, well-formatted, wrong work.
COMPACTION_ORDER = (
    "history",
    "memory",
    "retrieved_context",
    "tools",
    "skills",
    "task_input",
)


@dataclass(slots=True)
class ContextBudget:
    """The window, split into sections."""

    max_tokens: int = 32_000
    reserve_for_output: int = 4_000
    system_tokens: int = 0
    task_tokens: int = 0
    tools_tokens: int = 0
    skills_tokens: int = 0
    memory_tokens: int = 0
    retrieved_tokens: int = 0
    history_tokens: int = 0
    used: int = 0

    @property
    def available(self) -> int:
        return max(0, self.max_tokens - self.reserve_for_output)

    @property
    def remaining(self) -> int:
        return max(0, self.available - self.used)

    def report(self) -> dict[str, int]:
        return {
            "max_tokens": self.max_tokens,
            "reserve_for_output": self.reserve_for_output,
            "available": self.available,
            "used": self.used,
            "remaining": self.remaining,
            "system": self.system_tokens,
            "task": self.task_tokens,
            "tools": self.tools_tokens,
            "skills": self.skills_tokens,
            "memory": self.memory_tokens,
            "retrieved": self.retrieved_tokens,
        }


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class ContextBuilderInput:
    """Everything the builder needs. Assembled by the application layer."""

    actor: Actor
    task: AgentTask
    system_instructions: str
    role_name: str = ""
    #: Who this agent may hand work to: `(name, what they do)`. Supplied by the
    #: control plane from the organisation tree, because an agent cannot delegate
    #: to a name it was never given — and a model asked to pick one will invent a
    #: plausible department rather than say "I do not know".
    delegate_targets: tuple[tuple[str, str], ...] = ()
    delegate_options: tuple[DelegateOption, ...] = ()
    #: Ceiling on tool calls for this run, from settings. It lives here rather
    #: than as a constant in `BudgetEnvelope` so there is one number, set in one
    #: place, and raising it is a configuration change rather than a code change.
    #:
    #: Matches `settings.max_tool_calls`. Three places carried this number and
    #: they disagreed during this session, which is the failure mode a duplicated
    #: constant guarantees: right in the path someone tested, wrong in the rest.
    max_tool_calls: int = 48
    organization_id: str = ""
    org_unit_id: str | None = None
    tools: list[ToolContract] = field(default_factory=list)
    skills: list[SkillContract] = field(default_factory=list)
    memory: MemoryScope | None = None
    retrieved_context: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    delegation_path: DelegationPath = field(default_factory=DelegationPath)
    delegation_limits: DelegationLimits | None = None
    max_tokens: int = 200_000
    max_cost_usd: float = 5.0
    max_runtime_s: int = 900
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_LOW_RISK_AUTONOMOUS
    run_mode: RunMode = RunMode.LIVE
    model_profile: str = "default"
    deadline: Any = None
    agent_definition_version: int = 1
    policy_version: int = 1
    workflow_version: str = "v1"
    data_classification: DataClassification = DataClassification.INTERNAL
    context_window_tokens: int = 32_000
    reserve_for_output: int = 4_000


class ContextBuilder:
    """Builds an `AgentContext` within a token budget.

    Nothing here reads the database or the network. It is handed already-scoped
    material and assembles it, which is why the "what can this agent see"
    question is answerable by reading one function.
    """

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self._budget = budget or ContextBudget()
        # Per instance, not a class attribute: a mutable default on the class is
        # shared by every builder, so two concurrent builds would overwrite each
        # other's overflow report and the execution record would describe
        # someone else's compaction.
        self._overflow_report: dict[str, Any] = {}

    def build(self, data: ContextBuilderInput) -> AgentContext:
        if not data.system_instructions.strip():
            msg = "system_instructions must not be empty"
            raise ValidationError(msg, details={"field": "system_instructions"})

        budget = ContextBudget(
            max_tokens=data.context_window_tokens,
            reserve_for_output=data.reserve_for_output,
        )
        budget.system_tokens = estimate_tokens(data.system_instructions)
        budget.task_tokens = estimate_tokens(_render_task(data.task))
        budget.tools_tokens = estimate_tokens(_render_tools(data.tools))
        budget.skills_tokens = estimate_tokens(_render_skills(data.skills))
        budget.memory_tokens = estimate_tokens(_render_scope(data.memory) if data.memory else "")
        budget.retrieved_tokens = sum(estimate_tokens(c) for c in data.retrieved_context)
        budget.history_tokens = sum(estimate_tokens(h) for h in data.history)
        budget.used = (
            budget.system_tokens
            + budget.task_tokens
            + budget.tools_tokens
            + budget.skills_tokens
            + budget.memory_tokens
            + budget.retrieved_tokens
            + budget.history_tokens
        )

        tools, skills, retrieved, history = _compact(budget, data, self._overflow_report)

        envelope = BudgetEnvelope(
            max_tokens=data.max_tokens,
            max_cost_usd=Money(str(data.max_cost_usd)),
            max_runtime_s=data.max_runtime_s,
            max_tool_calls=data.max_tool_calls,
        )
        return AgentContext(
            actor=data.actor,
            task=data.task,
            system_instructions=data.system_instructions,
            role_name=data.role_name,
            delegate_targets=data.delegate_targets,
            delegate_options=data.delegate_options,
            organization_id=data.organization_id,
            org_unit_id=data.org_unit_id,
            authorized_tools=tuple(tools),
            authorized_skills=tuple(skills),
            memory=data.memory,
            delegation_path=data.delegation_path,
            delegation_limits=data.delegation_limits or DelegationLimits.platform_default(),
            budget=envelope,
            autonomy_level=data.autonomy_level,
            run_mode=data.run_mode,
            model_profile=data.model_profile,
            deadline=data.deadline,
            retrieved_context=tuple(retrieved),
            history=tuple(history),
            data_classification=data.data_classification,
            agent_definition_version=data.agent_definition_version,
            policy_version=data.policy_version,
            workflow_version=data.workflow_version,
        )

    @property
    def overflow_report(self) -> dict[str, Any]:
        return self._overflow_report


def _compact(
    budget: ContextBudget, data: ContextBuilderInput, report: dict[str, Any]
) -> tuple[list[ToolContract], list[SkillContract], list[str], list[str]]:
    """Drop or truncate sections until the context fits.

    Drops in `COMPACTION_ORDER`, and the report records exactly what was lost so
    the execution record shows that a run happened with a partial context. A
    silent truncation would make a bad answer impossible to explain afterwards.
    """
    tools = list(data.tools)
    skills = list(data.skills)
    retrieved = list(data.retrieved_context)
    history = list(data.history)
    dropped: dict[str, int] = {}

    def usage() -> int:
        """Recompute the total from what is actually in the context.

        Recomputed rather than decremented. An incremental `used -= tokens`
        needs the running total to be exactly right at every step, and one
        place that forgets a term makes every later subtraction wrong by that
        amount. Here the source of truth is the current contents, so the number
        cannot drift.
        """
        return (
            budget.system_tokens
            + budget.task_tokens
            + (estimate_tokens(_render_tools(tools)) if tools else 0)
            + (estimate_tokens(_render_skills(skills)) if skills else 0)
            + budget.memory_tokens
            + sum(estimate_tokens(c) for c in retrieved)
            + sum(estimate_tokens(h) for h in history)
        )

    def over() -> bool:
        return usage() > budget.available

    if over():
        # History goes first: it is the most recent material and the cheapest
        # to re-derive, because the parent task's result is already recorded.
        while history and over():
            removed = history.pop()
            dropped["history"] = dropped.get("history", 0) + estimate_tokens(removed)
        if over():
            while retrieved and over():
                removed = retrieved.pop()
                dropped["memory"] = dropped.get("memory", 0) + estimate_tokens(removed)
        if over() and len(tools) > 1:
            # At least one tool always survives: an agent with no tools cannot do
            # the work, and silently ending up with none is worse than a shorter
            # list.
            while len(tools) > 1 and over():
                tools.pop()
                dropped["tools"] = dropped.get("tools", 0) + 1
        if over() and len(skills) > 1:
            while len(skills) > 1 and over():
                skills.pop()
                dropped["skills"] = dropped.get("skills", 0) + 1
            if over():
                skills = skills[:1]

    budget.used = usage()
    report.clear()
    report.update(dropped)
    return tools, skills, retrieved, history


def _render_task(task: AgentTask) -> str:
    return (
        f"Task {task.task_id}\n"
        f"Goal: {task.goal}\n"
        f"Type: {task.task_type.value}\n"
        f"Input: {task.input}" + (f"\nConstraints: {task.constraints}" if task.constraints else "")
    )


def _render_tools(tools: list[ToolContract]) -> str:
    return "\n".join(
        f"- {t.name}({', '.join(t.input_schema.get('properties', {}))}) "
        f"risk={t.risk.value} approval={t.requires_approval}: {t.description}"
        for t in tools
    )


def _render_skills(skills: list[SkillContract]) -> str:
    return "\n".join(f"- {s.name}@{s.version}: {s.description}" for s in skills)


def _render_scope(scope: MemoryScope) -> str:
    return (
        f"Memory scope: org={scope.organization_id} "
        f"units={[str(u) for u in scope.org_unit_ids]} "
        f"max_classification={scope.max_classification.value} "
        f"max_results={scope.max_results}"
    )


def actor_for_agent(
    agent_id: str,
    organization_id: str,
    *,
    org_unit_id: str | None = None,
    role_id: str | None = None,
    display_name: str = "",
) -> Actor:
    """Build the `Actor` an agent acts as.

    Constructed here rather than passed in by the runtime, because the runtime's
    opinion of who it is must never be the answer. Identity comes from the
    registry row the control plane loaded.
    """
    return Actor(
        id=agent_id,
        kind=ActorType.AGENT,
        display_name=display_name,
        organization_id=organization_id,
        org_unit_id=org_unit_id,
        role_id=role_id,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "COMPACTION_ORDER",
    "ContextBudget",
    "ContextBuilder",
    "ContextBuilderInput",
    "actor_for_agent",
    "estimate_tokens",
]
