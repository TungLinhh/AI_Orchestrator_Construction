"""Delegation graph and bounded autonomy.

This module is the answer to "what stops A -> B -> C -> A forever".

Four independent limits, because each catches a different failure:

    depth          an unbounded chain down a hierarchy
    fan-out        one agent spawning 500 siblings at once
    active descendants  many levels deep but wide at every level
    budget / time  work that stays shallow but never ends

Plus explicit cycle detection over the ancestor path. Depth alone does not
catch a cycle: A->B->C->A stays at depth 3 while looping forever, because the
walk resets when the task is re-queued.

Everything here is a pure function. The caller is responsible for persisting the
`DelegationPath` it passed in, which is what makes the check auditable after
the fact rather than merely protective during the call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ai_orchestrator.domain.errors import CycleDetected, PreconditionError, ValidationError
from ai_orchestrator.domain.ids import AgentId, TaskId


@dataclass(frozen=True, slots=True)
class DelegationLimits:
    """Hard ceilings. An agent cannot raise its own — the control plane clamps."""

    max_depth: int
    max_fanout: int
    max_active_descendants: int
    max_tokens: int
    max_cost_usd: float
    max_runtime_s: int

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_fanout", "max_active_descendants", "max_runtime_s"):
            if getattr(self, name) < 0:
                msg = f"{name} must be >= 0"
                raise ValidationError(msg, details={"field": name})
        if self.max_tokens < 0:
            msg = "max_tokens must be >= 0"
            raise ValidationError(msg, details={"field": "max_tokens"})
        if self.max_cost_usd < 0:
            msg = "max_cost_usd must be >= 0"
            raise ValidationError(msg, details={"field": "max_cost_usd"})

    @classmethod
    def platform_default(cls) -> DelegationLimits:
        # `max_fanout` was 8, and **that number was smaller than the organisation it was
        # supposed to bound**. Measured on a real run of the seeded company: the chief
        # delegated successfully eight times and then asked for a ninth, was refused with
        # `fan-out 8 reached the limit of 8`, retried until its 48 requests were spent,
        # and the run ended
        #
        #     task.failed  category=budget_error
        #       reason='the agent exceeded its turn budget and was stopped'
        #
        # which names the symptom. The chief had ten peers to address -- three offices
        # and seven departments -- and a ceiling of eight meant the *ceiling*, not the
        # work, was what stopped it.
        #
        # 16, for two reasons, and both are about the control rather than generosity. It
        # matches `max_active_descendants`, so the two ceilings cannot disagree about how
        # wide one agent's subtree may be. And it is far enough above ten that on a real
        # run the binding constraint is the work rather than the setting. Lowering it
        # below the organisation's own width reintroduces this failure with a smaller
        # number in the message, which is why
        # `TestTheCeilingIsNotTheBindingConstraint` asserts the *relationship* and not
        # the literal.
        return cls(
            max_depth=4,
            max_fanout=16,
            max_active_descendants=16,
            max_tokens=200_000,
            max_cost_usd=5.0,
            max_runtime_s=900,
        )

    def clamp_to(self, outer: DelegationLimits) -> DelegationLimits:
        """A child may only ever receive a *narrower* envelope than its parent.

        Prevents the classic escalation where a subagent hands itself a larger
        budget than the agent that spawned it.
        """
        return DelegationLimits(
            max_depth=min(self.max_depth, outer.max_depth),
            max_fanout=min(self.max_fanout, outer.max_fanout),
            max_active_descendants=min(self.max_active_descendants, outer.max_active_descendants),
            max_tokens=min(self.max_tokens, outer.max_tokens),
            max_cost_usd=min(self.max_cost_usd, outer.max_cost_usd),
            max_runtime_s=min(self.max_runtime_s, outer.max_runtime_s),
        )


@dataclass(frozen=True, slots=True)
class DelegationStep:
    """One hop in the chain that produced the current task."""

    agent_id: AgentId
    task_id: TaskId
    depth: int

    def to_dict(self) -> dict[str, object]:
        return {"agent_id": str(self.agent_id), "task_id": str(self.task_id), "depth": self.depth}


@dataclass(slots=True)
class DelegationPath:
    """The full ancestor chain, root first.

    Carrying the whole path (not just a depth counter) is what makes cycle
    detection possible and what gets written to the audit row so a reviewer can
    see the actual route a request took.
    """

    steps: list[DelegationStep] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return len(self.steps)

    @property
    def agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(s.agent_id for s in self.steps)

    def would_cycle(self, target: AgentId) -> bool:
        return target in self.agent_ids

    def extend(self, agent_id: AgentId, task_id: TaskId) -> DelegationPath:
        return DelegationPath(
            steps=[*self.steps, DelegationStep(agent_id, task_id, len(self.steps) + 1)]
        )

    def to_dict(self) -> list[dict[str, object]]:
        return [s.to_dict() for s in self.steps]

    @classmethod
    def root(cls, agent_id: AgentId, task_id: TaskId) -> DelegationPath:
        """A human-submitted task: the submitting agent is the root, depth 1."""
        return cls(steps=[DelegationStep(agent_id, task_id, 1)])


@dataclass(frozen=True, slots=True)
class FanoutRequest:
    agent_id: AgentId
    path: DelegationPath
    current_fanout: int
    current_active_descendants: int


@dataclass(frozen=True, slots=True)
class DelegationVerdict:
    allowed: bool
    reason: str
    effective_limits: DelegationLimits
    #: **True when retrying differently cannot help.**
    #:
    #: A denial for a structural reason — this task's fan-out, depth or active-descendant
    #: budget is spent — is terminal for the task. A denial for a specific request — a
    #: cycle, self-delegation, a duplicate intent — is not: a different target or a
    #: different objective is a different request, and may well be allowed.
    #:
    #: This is the difference between telling a coordinator *"no"* and telling it *"no,
    #: and there is nothing else to try"*, and it is not cosmetic. A real procurement run
    #: was handed the bare refusal 358 times and ended `budget_error` with sixteen
    #: children dispatched and none executed, purely because the message never said
    #: which refusals had a different answer waiting. The flag carries that knowledge
    #: from the rule that knows it to the caller that has to write it down.
    terminal: bool = False

    def __bool__(self) -> bool:
        return self.allowed


def authorize_delegation(
    *,
    source: AgentId,
    target: AgentId,
    path: DelegationPath,
    requested: DelegationLimits | None,
    parent_limits: DelegationLimits,
    platform_limits: DelegationLimits,
    current_fanout: int = 0,
    current_active_descendants: int = 0,
) -> DelegationVerdict:
    """Decide whether `source` may delegate to `target`.

    Returns a verdict rather than raising so the caller can record a denial in
    the audit log with a reason instead of only surfacing an exception.
    """
    ceiling = parent_limits.clamp_to(platform_limits)

    # 0. Self-delegation is checked before the cycle test so the denial reason
    #    names the actual mistake rather than reporting a one-hop "cycle".
    if source == target:
        return DelegationVerdict(
            allowed=False,
            reason="an agent may not delegate to itself",
            effective_limits=ceiling,
        )

    # 1. Cycle check. A cycle is the failure with the worst consequences
    #    (unbounded self-replication), so it outranks the cheaper numeric checks.
    if path.would_cycle(target):
        return DelegationVerdict(
            allowed=False,
            reason=(
                f"delegation cycle: {target} is already on the current delegation path "
                f"({' -> '.join(str(a) for a in path.agent_ids)})"
            ),
            effective_limits=ceiling,
        )

    if path.depth >= ceiling.max_depth:
        return DelegationVerdict(
            allowed=False,
            reason=f"delegation depth {path.depth} reached the limit of {ceiling.max_depth}",
            effective_limits=ceiling,
            terminal=True,
        )

    if current_fanout >= ceiling.max_fanout:
        return DelegationVerdict(
            allowed=False,
            reason=(
                f"fan-out {current_fanout} reached the limit of {ceiling.max_fanout}. "
                "This task has already delegated as widely as it is allowed to, so no "
                "further agent and no reworded objective will be accepted."
            ),
            effective_limits=ceiling,
            terminal=True,
        )

    if current_active_descendants >= ceiling.max_active_descendants:
        return DelegationVerdict(
            allowed=False,
            reason=(
                f"active descendants {current_active_descendants} reached the limit of "
                f"{ceiling.max_active_descendants}. Too much delegated work is already "
                "in flight, so this task cannot delegate again until some of it finishes."
            ),
            effective_limits=ceiling,
            terminal=True,
        )

    if source == target:
        return DelegationVerdict(
            allowed=False,
            reason="an agent may not delegate to itself",
            effective_limits=ceiling,
        )

    effective = requested.clamp_to(ceiling) if requested else ceiling
    return DelegationVerdict(
        allowed=True, reason="within delegation limits", effective_limits=effective
    )


def require_delegation_allowed(**kwargs: object) -> DelegationLimits:
    """`authorize_delegation` that raises. For use inside a workflow activity."""
    verdict = authorize_delegation(**kwargs)  # type: ignore[arg-type]
    if not verdict.allowed:
        if "cycle" in verdict.reason:
            raise CycleDetected(verdict.reason, details={"reason": verdict.reason})
        raise PreconditionError(verdict.reason, details={"reason": verdict.reason})
    return verdict.effective_limits


# -------------------------------------------------------- duplicate detection --
#: How many leading normalised tokens identify an instruction. Migration `0026` carries the
#: measurement.
#:
#: **It is 16, and the first value tried was 12, which was wrong.** Two things had to be
#: measured rather than chosen, and both were got wrong first:
#:
#: 1. **`task_fingerprint` sorts its tokens, so a prefix of a *sorted* form is useless.** The
#:    clause a model appends is at the end of the sentence but lands in the middle once sorted,
#:    so a 12-token sorted prefix moved when the extra words arrived and the duplicate got
#:    through. The tokens here are normalised and **kept in order**.
#: 2. **Too short a prefix collides on different work.** The two genuinely different
#:    instructions measured here share their first 15 tokens -- they are the same project and
#:    the same verb -- and diverge at token **16** (*"chất lượng, đầy đủ"* against *"tính đầy
#:    đủ, chính xác"*). So any prefix of 15 or fewer refuses real delegation.
#:
#: Measured, on the five real children: both same-work pairs are exact token prefixes of one
#: another (diverge at *none*), the different pair diverges at 16, and every length from **16
#: to 28** satisfies all three. 16 is the lower edge of that measured window, with the shortest
#: same-work pair 29 tokens long -- so there is 12 tokens of headroom before the two could
#: meet, and 12 tokens of slack after the different work has already separated.
#:
#: **This is still one tenant's corpus.** The window is measured, not derived, and a corpus
#: whose instructions separate later or restate later would need a different number. The
#: alternative -- asking the model to carry a stable delegation id -- needs no constant at
#: all, and is recorded as the better design.
INTENT_PREFIX_TOKENS = 16


def intent_fingerprint(
    *, organization_id: str, task_type: str, goal: str, owner_agent_id: str
) -> str:
    """A key for "this parent already gave this agent this instruction", wording-insensitively.

    **Not the same question as `task_fingerprint`**, which hashes the whole normalised goal
    *sorted*. That one is right for "has an equivalent task ever been created anywhere" and
    wrong for the case measured here: a model re-read its own results and re-asked the same
    work with "aggregate the artifacts you have already created" appended, so the whole-goal
    hash moved and the duplicate got through. Four times, on one parent.

    Normalised the same way -- lowercased, punctuation to spaces, stop words dropped -- and then
    **the first `INTENT_PREFIX_TOKENS` in their original order**, so punctuation, casing and
    word order at the front do not move the key, and an appended clause at the end does not
    reach it.

    The owner agent is in the payload, because "this agent was already given this" must not
    match "some other agent was already given this": the same instruction to two departments is
    two pieces of work, not one duplicated.
    """
    import hashlib
    import re

    normalised_goal = re.sub(r"[^\w\s]", " ", goal.lower())
    ordered = [t for t in normalised_goal.split() if t and t not in _STOP_WORDS]
    prefix = " ".join(ordered[:INTENT_PREFIX_TOKENS])
    payload = f"{organization_id}\x1f{task_type}\x1f{owner_agent_id}\x1f{prefix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def task_fingerprint(
    *,
    organization_id: str,
    task_type: str,
    goal: str,
    scope: Sequence[str] = (),
) -> str:
    """Stable hash of a task's *intent*.

    Used to detect two agents independently requesting the same work. The
    normaliser is intentionally lossy (case, punctuation, whitespace, stop
    words): the goal is "Prepare a market analysis." vs "prepare the market
    analysis" and those must collide, while genuinely different goals must not.
    """
    import hashlib
    import re

    normalized_goal = re.sub(r"[^\w\s]", " ", goal.lower())
    tokens = [t for t in normalized_goal.split() if t and t not in _STOP_WORDS]
    tokens.sort()
    scope_key = "|".join(sorted(scope))
    payload = f"{organization_id}\x1f{task_type}\x1f{' '.join(tokens)}\x1f{scope_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class DuplicateAssessment:
    is_duplicate: bool
    existing_task_id: TaskId | None
    reason: str


def assess_duplicate_work(
    *,
    fingerprint: str,
    active_tasks: Sequence[tuple[TaskId, str]],
    allow_parallel: bool = False,
) -> DuplicateAssessment:
    """Decide whether to reuse, merge, or proceed.

    `active_tasks` is the set of (task_id, fingerprint) currently in a
    non-terminal state for the same organization. `allow_parallel` exists
    because sometimes two agents genuinely must do the same thing at once — and
    that has to be an explicit decision, not an accident of a race.
    """
    if allow_parallel:
        return DuplicateAssessment(False, None, "parallel work explicitly allowed")
    for task_id, existing_fp in active_tasks:
        if existing_fp == fingerprint:
            return DuplicateAssessment(
                True,
                task_id,
                f"an equivalent active task already exists ({task_id}); reuse or merge it",
            )
    return DuplicateAssessment(False, None, "no equivalent active task")


__all__ = [
    "DelegationLimits",
    "DelegationPath",
    "DelegationStep",
    "DelegationVerdict",
    "DuplicateAssessment",
    "FanoutRequest",
    "assess_duplicate_work",
    "authorize_delegation",
    "require_delegation_allowed",
    "task_fingerprint",
]
